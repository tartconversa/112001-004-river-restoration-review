"""领域规则单元测试（纯标准库 unittest）。

覆盖四条不可违背的规则：
1. 季节/河段分组，绝不跨组合并；
2. 重复社区反馈聚类，只计一项独立证据；
3. 处置记录只能支撑其实际覆盖的污染源/区段；
4. 证据矛盾或覆盖不全时保留不确定性并阻止“已恢复”。
另含审计日志、增量差异与端到端 CLI 流程测试。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from riverreview.clustering import cluster_reports, normalize_description
from riverreview.coverage import source_is_addressed, unaddressed_sources
from riverreview.events import EventLog
from riverreview.ingest import (
    IngestError,
    load_disposals,
    load_evidence,
)
from riverreview.models import (
    CONTRADICTORY,
    FAIL,
    MISSING,
    NOT_RECOVERED,
    PASS,
    RECOVERED,
    STALE,
    UNCERTAIN,
    ActionItem,
    CommunityReport,
    Criterion,
    Disposal,
    Evidence,
    PollutionSource,
    Reach,
)
from riverreview.review import evaluate_reach, roll_up_project, run_review
from riverreview.seasons import season_key, season_of
from riverreview.store import Workspace

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
AS_OF = date(2026, 7, 1)


def make_reach(reach_id="R-X", project="P1", up=0.0, down=10.0) -> Reach:
    return Reach(reach_id, "Clearwater", up, down, project)


def make_criterion(cid="K1", project="P1", measure="DO",
                   operator="ge", threshold=6.0) -> Criterion:
    return Criterion(cid, project, measure, operator, threshold,
                     date(2026, 1, 1), None)


def make_evidence(eid, reach_id="R-X", day=date(2026, 6, 1),
                  measure="DO", value=7.0, etype="water") -> Evidence:
    return Evidence(eid, reach_id, day, etype, measure, value,
                    "mg_l", "SRC", season_of(day))


class SeasonTests(unittest.TestCase):
    def test_season_assignment_and_ordering(self):
        self.assertEqual(season_of(date(2026, 5, 10)), "spring")
        self.assertEqual(season_of(date(2026, 6, 15)), "summer")
        self.assertEqual(season_of(date(2026, 12, 31)), "winter")
        # 12 月与次年 1-2 月归入同一冬季键
        self.assertEqual(season_key(date(2026, 12, 15)),
                         season_key(date(2027, 1, 10)))
        # 今年夏季晚于今年春季
        self.assertGreater(season_key(date(2026, 6, 1)),
                           season_key(date(2026, 5, 1)))


class SeasonIsolationTests(unittest.TestCase):
    """规则 1：不同季节的观测不得并入同一基线。"""

    def test_seasons_are_evaluated_separately_not_averaged(self):
        # 春季不达标 (5.2)，夏季达标 (7.6)：简单平均会得到 6.4 并误判达标
        evidence = [
            make_evidence("E1", day=date(2026, 4, 1), value=5.2),
            make_evidence("E2", day=date(2026, 6, 1), value=7.6),
        ]
        conclusion = evaluate_reach(
            make_reach(), [make_criterion()], evidence, [], [], [], [],
            AS_OF, max_age_days=120)
        result = conclusion.criterion_results[0]
        self.assertEqual(result.status, PASS)  # 以最新季节为准
        self.assertEqual(len(result.season_results), 2)
        # 春季失败作为历史留痕，不与夏季平均，也不抹掉最新达标
        self.assertEqual(
            [s.status for s in result.season_results], [FAIL, PASS])
        self.assertIn("SEASON_SPLIT", result.reason_codes)

    def test_contradiction_within_one_season_is_flagged(self):
        evidence = [
            make_evidence("E1", day=date(2026, 6, 1), value=5.8),
            make_evidence("E2", day=date(2026, 6, 20), value=7.1),
        ]
        conclusion = evaluate_reach(
            make_reach(), [make_criterion()], evidence, [], [], [], [],
            AS_OF, max_age_days=120)
        result = conclusion.criterion_results[0]
        self.assertEqual(result.status, CONTRADICTORY)
        self.assertEqual(conclusion.status, UNCERTAIN)
        self.assertIn("CONTRADICTORY_EVIDENCE", conclusion.reason_codes)

    def test_reaches_are_never_pooled(self):
        evidence = [
            make_evidence("E1", reach_id="R-X", value=5.0),
            make_evidence("E2", reach_id="R-Y", value=9.0),
        ]
        conclusion = evaluate_reach(
            make_reach("R-X"), [make_criterion()], evidence,
            [], [], [], [], AS_OF, max_age_days=120)
        # R-X 只看到自己的 5.0，不被 R-Y 的 9.0“拉高”
        self.assertEqual(conclusion.criterion_results[0].status, FAIL)

    def test_missing_and_stale_keep_uncertainty(self):
        conclusion = evaluate_reach(
            make_reach(), [make_criterion()], [], [], [], [], [],
            AS_OF, max_age_days=120)
        self.assertEqual(conclusion.criterion_results[0].status, MISSING)
        self.assertEqual(conclusion.status, UNCERTAIN)

        old = [make_evidence("E1", day=AS_OF - timedelta(days=200), value=9.0)]
        conclusion = evaluate_reach(
            make_reach(), [make_criterion()], old, [], [], [], [],
            AS_OF, max_age_days=120)
        self.assertEqual(conclusion.criterion_results[0].status, STALE)
        self.assertEqual(conclusion.status, UNCERTAIN)


class ClusteringTests(unittest.TestCase):
    """规则 2：重复反馈可关联，但不重复计数。"""

    @staticmethod
    def _report(rid, reach="R-X", day=date(2026, 6, 18),
                desc="oil sheen", status="open", group=None):
        return CommunityReport(rid, reach, day, desc, status,
                               "team", None, group)

    def test_duplicate_reports_form_one_open_cluster(self):
        reports = [
            self._report("C1"),
            self._report("C2", day=date(2026, 6, 19)),
        ]
        clusters = cluster_reports(reports)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].member_ids, ["C1", "C2"])
        self.assertTrue(clusters[0].is_open)

    def test_distinct_issues_remain_separate_evidence(self):
        reports = [
            self._report("C1", desc="oil sheen"),
            self._report("C2", desc="dead fish smell"),
        ]
        clusters = cluster_reports(reports)
        self.assertEqual(len(clusters), 2)

    def test_window_boundary_and_closed_cluster(self):
        reports = [
            self._report("C1", day=date(2026, 6, 1)),
            self._report("C2", day=date(2026, 7, 15)),  # 超过 30 天窗
        ]
        self.assertEqual(len(cluster_reports(reports)), 2)

        reports = [
            self._report("C1", status="complete"),
            self._report("C2", day=date(2026, 6, 2), status="dismissed"),
        ]
        cluster = cluster_reports(reports)[0]
        self.assertFalse(cluster.is_open)

    def test_explicit_group_and_cross_reach_rejection(self):
        reports = [
            self._report("C1", group="G1"),
            self._report("C2", day=date(2026, 7, 10), group="G1"),
        ]
        self.assertEqual(len(cluster_reports(reports)), 1)

        reports = [
            self._report("C1", reach="R-X", group="G1"),
            self._report("C2", reach="R-Y", group="G1"),
        ]
        with self.assertRaises(ValueError):
            cluster_reports(reports)

    def test_open_cluster_blocks_recovery(self):
        reports = [self._report("C1")]
        evidence = [make_evidence("E1", value=9.0)]
        conclusion = evaluate_reach(
            make_reach(), [make_criterion()], evidence,
            [], [], [], cluster_reports(reports),
            AS_OF, max_age_days=120)
        self.assertEqual(conclusion.status, NOT_RECOVERED)
        self.assertIn("OPEN_COMMUNITY_CLUSTER", conclusion.reason_codes)
        self.assertEqual(conclusion.open_cluster_ids, ["CL-001"])

    def test_normalization_ignores_punctuation_and_case(self):
        self.assertEqual(normalize_description("Oil-Sheen! Near outfall."),
                         normalize_description("oil sheen near outfall"))


class CoverageTests(unittest.TestCase):
    """规则 3：处置只能支持其实际覆盖的污染源/区段。"""

    def setUp(self):
        self.reaches = [
            Reach("R-X", "r", 0.0, 10.0, "P1"),
            Reach("R-Y", "r", 10.0, 20.0, "P1"),
        ]
        self.source = PollutionSource("S1", "R-X", 5.0, "oil", None)

    def test_explicit_completed_disposal_addresses_source(self):
        disposal = Disposal("D1", "S1", "R-X", 4.0, 6.0,
                            "complete", date(2026, 6, 1), "W1")
        ok, did = source_is_addressed(self.source, [disposal], AS_OF)
        self.assertTrue(ok)
        self.assertEqual(did, "D1")

    def test_in_progress_disposal_does_not_count(self):
        disposal = Disposal("D1", "S1", "R-X", 4.0, 6.0,
                            "in_progress", None, "W1")
        ok, _ = source_is_addressed(self.source, [disposal], AS_OF)
        self.assertFalse(ok)

    def test_mileage_coverage_only_within_same_reach(self):
        # 无 source_id，里程覆盖到 R-X 内的 S1
        covering = Disposal("D1", None, "R-X", 4.0, 6.0,
                            "complete", date(2026, 6, 1), "W1")
        ok, did = source_is_addressed(self.source, [covering], AS_OF)
        self.assertTrue(ok)
        self.assertEqual(did, "D1")
        # 位于 R-Y 的处置，即使里程数字相近也不得跨河段支撑
        other_reach = Disposal("D2", None, "R-Y", 4.0, 6.0,
                               "complete", date(2026, 6, 1), "W2")
        ok, _ = source_is_addressed(self.source, [other_reach], AS_OF)
        self.assertFalse(ok)

    def test_completion_after_review_date_does_not_count(self):
        disposal = Disposal("D1", "S1", "R-X", 4.0, 6.0,
                            "complete", AS_OF + timedelta(days=2), "W1")
        ok, _ = source_is_addressed(self.source, [disposal], AS_OF)
        self.assertFalse(ok)

    def test_unaddressed_source_blocks_recovery(self):
        evidence = [make_evidence("E1", value=9.0)]
        conclusion = evaluate_reach(
            make_reach(), [make_criterion()], evidence,
            [self.source], [], [], [], AS_OF, max_age_days=120)
        self.assertEqual(conclusion.status, NOT_RECOVERED)
        self.assertEqual(conclusion.unaddressed_sources, ["S1"])
        pending = unaddressed_sources([self.source], [], AS_OF)
        self.assertEqual(pending, {"S1": None})

    def test_disposal_outside_reach_rejected_at_ingest(self):
        bad = DATA.parent / "tests_tmp_coverage.csv"
        bad.write_text(
            "disposal_id,source_id,reach_id,covered_from_km,covered_to_km,"
            "status,completed_at,evidence_ref\n"
            "DBAD,,R-X,9.0,11.0,complete,2026-06-01,W\n", encoding="utf-8")
        try:
            with self.assertRaises(IngestError):
                load_disposals(bad, reaches=self.reaches, sources=[self.source])
        finally:
            bad.unlink(missing_ok=True)


class RecoveryGuardTests(unittest.TestCase):
    """规则 4：任何不确定性都阻止 recovered；项目汇总同理。"""

    def test_criterion_outside_validity_window_is_skipped(self):
        past = Criterion("KOLD", "P1", "DO", "ge", 6.0,
                         date(2025, 1, 1), date(2025, 12, 31))
        conclusion = evaluate_reach(
            make_reach(), [past], [make_evidence("E1", value=9.0)],
            [], [], [], [], AS_OF, max_age_days=120)
        # 准则已失效：没有可评估准则，结论保持不确定
        self.assertEqual(conclusion.status, UNCERTAIN)
        self.assertIn("OUTSIDE_VALIDITY", conclusion.reason_codes)
        self.assertEqual(conclusion.criterion_results, [])

    def test_unknown_reach_foreign_key_rejected_at_ingest(self):
        import csv
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "evidence.csv"
            with bad.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["evidence_id", "reach_id", "date",
                                 "evidence_type", "measure", "value",
                                 "unit", "source_ref", "season"])
                writer.writerow(["E9", "R-NOPE", "2026-06-01", "water",
                                 "DO", "7", "mg_l", "S", ""])
            with self.assertRaises(IngestError):
                load_evidence(bad, season_resolver=season_of,
                              valid_reach_ids={"R-X"})

    def test_all_pass_and_no_blockers_allows_recovery(self):
        evidence = [
            make_evidence("E1", measure="DO", value=8.0),
            make_evidence("E2", measure="fish_species", value=9,
                          etype="ecology"),
        ]
        criteria = [
            make_criterion("K1", measure="DO", threshold=6.0),
            make_criterion("K2", measure="fish_species", threshold=8),
        ]
        conclusion = evaluate_reach(
            make_reach(), criteria, evidence, [], [], [], [],
            AS_OF, max_age_days=120)
        self.assertEqual(conclusion.status, RECOVERED)

    def test_open_high_risk_action_blocks_recovery(self):
        evidence = [make_evidence("E1", value=9.0)]
        item = ActionItem("A1", "R-X", AS_OF, "disposal", "cleanup",
                          "open", "team", None)
        conclusion = evaluate_reach(
            make_reach(), [make_criterion()], evidence,
            [], [], [item], [], AS_OF, max_age_days=120)
        self.assertNotEqual(conclusion.status, RECOVERED)
        self.assertIn("OPEN_HIGH_RISK_ACTION", conclusion.reason_codes)

    def test_project_rollup_requires_every_reach_recovered(self):
        from riverreview.models import ReachConclusion
        good = ReachConclusion("R-A", "P1", AS_OF.isoformat(), RECOVERED,
                               [], [], [], [], [], 1)
        bad = ReachConclusion("R-B", "P1", AS_OF.isoformat(), UNCERTAIN,
                              ["CONTRADICTORY_EVIDENCE"], [], [], [], [], 1)
        project = roll_up_project("P1", [good, bad], AS_OF)
        self.assertEqual(project.status, UNCERTAIN)
        project = roll_up_project("P1", [good], AS_OF)
        self.assertEqual(project.status, RECOVERED)


class EventLogTests(unittest.TestCase):
    def test_append_only_and_sequential(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = EventLog(Path(tmp) / "events.jsonl")
            log.append("a", {"x": 1})
            log.append("b", {"y": 2})
            events = log.read()
            self.assertEqual([e.seq for e in events], [1, 2])
            self.assertEqual(events[1].event_type, "b")
            # 文件确为逐行 JSON 追加
            lines = (Path(tmp) / "events.jsonl").read_text(
                encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(all(json.loads(line) for line in lines))


class WorkspaceEndToEndTests(unittest.TestCase):
    """通过真实夹具走完整工作区流程，并验证增量差异。"""

    def test_full_workspace_flow_with_fixtures(self):
        from riverreview.ingest import (
            load_criteria, load_evidence, load_policies, load_reaches,
            load_reports_actions, load_sources,
        )

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            ws = Workspace(root=state_dir)
            ws.reaches = load_reaches(DATA / "reaches.csv")
            ws.criteria = load_criteria(DATA / "review_criteria.csv")
            ws.evidence = load_evidence(DATA / "evidence.csv",
                                        season_resolver=season_of)
            ws.sources = load_sources(DATA / "pollution_sources.csv")
            ws.disposals = load_disposals(DATA / "disposals.csv",
                                          reaches=ws.reaches,
                                          sources=ws.sources)
            ws.reports, ws.actions = load_reports_actions(
                DATA / "reports_actions.csv")
            ws.policies = load_policies(DATA / "inspection_policy.csv")

            bundle = run_review(ws, AS_OF)
            statuses = {c.reach_id: c.status for c in bundle.reach_conclusions}
            self.assertEqual(statuses, {
                "R-A": RECOVERED,
                "R-B": NOT_RECOVERED,
                "R-C": UNCERTAIN,
            })
            # R-A 可降频，其余两段不允许
            flags = {f.reach_id: f.expected_mode for f in bundle.schedule_flags}
            self.assertEqual(flags.get("R-A"), "reduced")
            self.assertNotIn("R-B", flags)
            self.assertNotIn("R-C", flags)

            # 重复反馈只形成一个簇
            rb_clusters = [c for c in bundle.clusters if c.reach_id == "R-B"]
            self.assertEqual(len(rb_clusters), 1)
            self.assertEqual(set(rb_clusters[0].member_ids), {"C01", "C02"})

            # 持久化并重新加载，嵌套结论必须完整还原
            ws.store_bundle(bundle)
            ws.save()
            reloaded = Workspace.load(state_dir)
            self.assertEqual(
                reloaded.latest_reach_conclusions["R-C"].status, UNCERTAIN)
            self.assertTrue(
                reloaded.latest_reach_conclusions["R-A"].criterion_results)

    def test_duplicate_import_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(root=Path(tmp))
            ws.evidence = [make_evidence("E1")]
            with self.assertRaises(ValueError):
                ws.ensure_new_evidence_ids(["E1", "E2"])


class CliSmokeTests(unittest.TestCase):
    """以子进程方式运行 CLI，顺带验证 -I 隔离模式可用。"""

    def test_cli_init_review_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            def run(*args):
                code = (
                    "import sys; sys.path.insert(0, %r);"
                    "from riverreview.cli import main; sys.exit(main())"
                    % str(ROOT)
                )
                proc = subprocess.run(
                    [sys.executable, "-I", "-c", code,
                     "--state-dir", str(state_dir), *args],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return proc.stdout

            run("init", str(DATA))
            out = run("review", "--as-of", "2026-07-01")
            self.assertIn("不确定(禁止标记恢复)", out)
            self.assertIn("已达标(可申请降频)", out)
            out = run("ingest", str(ROOT / "examples" / "batch_july"),
                      "--as-of", "2026-07-05")
            self.assertIn("自动关联为同一线索", out)
            out = run("history", "--event-type", "conclusion_revised")
            self.assertIn("conclusion_revised", out)
            # 状态文件与事件日志均已落盘
            self.assertTrue((state_dir / "state.json").exists())
            self.assertTrue((state_dir / "events.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
