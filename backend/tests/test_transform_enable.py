"""转换依赖启用准入的回归测试：环、缺失依赖、冲突生产者、幂等与历史留存。"""
from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from app.services import transform as transform_service
from app.services.transform import TransformService
from app.store import store


class TransformEnableTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.service = TransformService()
        self.land = self._add(
            "TRAN_LAND", "RAW_ORDERS", "ODS_ORDERS", "10", "",
            "order_id:order_id,amount:amount,user_id:user_id",
        )
        self.dwd = self._add(
            "TRAN_DWD", "ODS_ORDERS", "DWD_ORDERS", "20", "TRAN_LAND",
            "ODS_ORDERS.order_id->DWD_ORDERS.order_id,ODS_ORDERS.amount->DWD_ORDERS.amount",
        )
        self.clean = self._add(
            "TRAN_CLEAN", "ODS_ORDERS,DWD_ORDERS", "CLEAN_ORDERS", "30", "TRAN_LAND,TRAN_DWD",
            "DWD_ORDERS.order_id->CLEAN_ORDERS.order_id,"
            "ODS_ORDERS.amount->CLEAN_ORDERS.total_amount",
        )

    def _add(self, code: str, inputs: str, outputs: str, order: str, deps: str, mapping: str):
        # 示例数据里已有同名节点时直接复用，避免测试自身制造重复节点编号。
        existing = next(
            (row for row in store.rows("transform") if row.get("节点编号") == code), None,
        )
        if existing is None:
            entry, missing = self.service.create_entry({
                "节点编号": code,
                "输入表名": inputs,
                "输出表名": outputs,
                "执行顺序": order,
                "依赖节点": deps,
                "字段映射": mapping,
            })
            self.assertEqual(missing, [])
            return entry
        existing.update({
            "输入表名": inputs,
            "输出表名": outputs,
            "执行顺序": order,
            "依赖节点": deps,
            "字段映射": mapping,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
        })
        return existing

    def _active_lineage(self):
        return [row for row in store.rows("lineage") if row.get("来源版本")]

    # ------------------------------------------------------------------
    def test_cycle_blocks_entire_group(self) -> None:
        self.dwd["依赖节点"] = "TRAN_LAND,TRAN_CLEAN"
        outcome = self.service.enable_nodes(["TRAN_CLEAN"])

        self.assertFalse(outcome["ok"])
        self.assertIn("环", outcome["message"])
        self.assertEqual(outcome["entry"]["结论"], "拒绝启用")
        cycles = [item for item in outcome["entry"]["错误"] if item["代码"] == "循环依赖"]
        self.assertTrue(cycles)
        cycle = cycles[0]
        self.assertEqual(cycle["节点"][0], cycle["节点"][-1])
        self.assertEqual(set(cycle["节点"]), {"TRAN_CLEAN", "TRAN_DWD"})

        # 被拒绝时不留下任何半张血缘图，节点状态也不变
        self.assertEqual(self._active_lineage(), [])
        self.assertEqual(self.clean["status"], "草稿")
        self.assertEqual(self.dwd["status"], "草稿")
        self.assertEqual(self.service.versions(), [])
        self.assertFalse(
            any(row.get("变更类型") == "启用编排投影血缘" for row in store.rows("auditlog"))
        )

    def test_missing_dependency_is_reported(self) -> None:
        self.clean["依赖节点"] = "TRAN_LAND,TRAN_DWD,TRAN_LOST"
        outcome = self.service.enable_nodes(["TRAN_CLEAN"])

        self.assertFalse(outcome["ok"])
        errors = outcome["entry"]["错误"]
        missing = [item for item in errors if item["代码"] == "缺失依赖"]
        self.assertTrue(missing)
        self.assertIn("TRAN_LOST", missing[0]["说明"])
        self.assertEqual(self._active_lineage(), [])

    def test_duplicate_producer_conflict_is_ordered_by_graph(self) -> None:
        self._add("TRAN_ALSO", "ODS_ORDERS", "DWD_ORDERS", "25", "TRAN_LAND", "amount:amount")
        self.clean["依赖节点"] = "TRAN_LAND,TRAN_DWD,TRAN_ALSO"
        outcome = self.service.enable_nodes(["TRAN_CLEAN"])

        self.assertFalse(outcome["ok"])
        conflicts = [item for item in outcome["entry"]["错误"] if item["代码"] == "同表多生产者"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["表"], ["DWD_ORDERS"])
        # 按依赖图顺序：执行顺序 20 的 TRAN_DWD 排在 25 的 TRAN_ALSO 之前
        self.assertEqual(conflicts[0]["节点"], ["TRAN_DWD", "TRAN_ALSO"])
        self.assertNotIn("CLEAN", conflicts[0]["节点"])
        self.assertEqual(self._active_lineage(), [])

    def test_active_producer_conflict_is_rejected(self) -> None:
        active = self._add("TRAN_ACTIVE", "RAW_OTHER", "CLEAN_ORDERS", "5", "", "amount:amount")
        self.assertTrue(self.service.enable_nodes(["TRAN_ACTIVE"])["ok"])

        outcome = self.service.enable_nodes(["TRAN_CLEAN"])
        self.assertFalse(outcome["ok"])
        conflicts = [item for item in outcome["entry"]["错误"] if item["代码"] == "同表多生产者"]
        self.assertTrue(any("TRAN_ACTIVE" in item["说明"] for item in conflicts))
        # 已生效版本的血缘不受失败提交影响：只有 TRAN_ACTIVE 自己的一条
        self.assertEqual(len(self._active_lineage()), 1)
        self.assertEqual(self._active_lineage()[0]["来源节点"], "TRAN_ACTIVE")
        self.assertEqual(self.clean["status"], "草稿")
        # 失败后再修好冲突，仍能正常启用
        active_entry = next(
            row for row in store.rows("transform") if row["节点编号"] == "TRAN_ACTIVE"
        )
        active_entry["输出表名"] = "CLEAN_STAGE"
        outcome = self.service.enable_nodes(["TRAN_CLEAN"])
        self.assertTrue(outcome["ok"], outcome["message"])
        self.assertEqual(len(self._active_lineage()), 5)

    def test_success_generates_plan_and_field_lineage(self) -> None:
        outcome = self.service.enable_nodes(["TRAN_CLEAN"])
        self.assertTrue(outcome["ok"], outcome["message"])

        plan = outcome["entry"]["执行计划"]
        self.assertEqual([item["节点编号"] for item in plan], ["TRAN_LAND", "TRAN_DWD", "TRAN_CLEAN"])

        relations = {row["关系编号"]: row for row in self._active_lineage()}
        self.assertIn("LINE_LAND_TO_CLEAN", relations)
        self.assertIn("LINE_DWD_TO_CLEAN", relations)
        land_to_clean = relations["LINE_LAND_TO_CLEAN"]
        self.assertEqual(land_to_clean["上游表名"], "ODS_ORDERS")
        self.assertEqual(land_to_clean["下游表名"], "CLEAN_ORDERS")
        self.assertEqual(land_to_clean["上游字段"], "amount")
        self.assertEqual(land_to_clean["下游字段"], "total_amount")
        # 传递血缘能回到原始输入字段
        self.assertIn("RAW_ORDERS.amount", land_to_clean["原始输入"])
        self.assertTrue(any(
            "RAW_ORDERS.amount → ODS_ORDERS.amount" in path
            for path in land_to_clean["传递路径"]
        ))

        self.assertEqual(self.clean["status"], "已启用")
        audit = store.rows("auditlog")[-1]
        self.assertEqual(audit["变更类型"], "启用编排投影血缘")
        self.assertIn("LINE_LAND_TO_CLEAN", audit["变更后值"])
        self.assertTrue(self.service.versions()[0]["计划"])

    def test_repeated_submission_only_takes_effect_once(self) -> None:
        first = self.service.enable_nodes(["TRAN_CLEAN"])
        self.assertTrue(first["ok"])
        count = len(self._active_lineage())

        second = self.service.enable_nodes(["TRAN_CLEAN", "TRAN_LAND", "TRAN_CLEAN"])
        self.assertTrue(second["ok"])
        self.assertEqual(second["结论"], "重复提交未再生效")
        self.assertEqual(len(self._active_lineage()), count)
        self.assertEqual(len(self.service.versions()), 1)
        self.assertEqual(len([
            row for row in store.rows("auditlog")
            if row.get("变更类型") == "启用编排投影血缘"
        ]), 1)

    def test_idempotency_key_replays_and_protects_scope(self) -> None:
        first = self.service.enable_nodes(["TRAN_CLEAN"], idempotency_key="enable-clean-1")
        replay = self.service.enable_nodes(["TRAN_CLEAN"], idempotency_key="enable-clean-1")
        self.assertTrue(first["ok"])
        self.assertTrue(replay["ok"])
        self.assertEqual(replay["结论"], "幂等复用")
        self.assertEqual(len(self.service.versions()), 1)

        other = self.service.enable_nodes(["TRAN_LAND"], idempotency_key="enable-clean-1")
        self.assertFalse(other["ok"])
        self.assertEqual(other["entry"]["错误"][0]["代码"], "幂等键冲突")
        self.assertEqual(len(self.service.versions()), 1)

    def test_concurrent_enable_generates_one_version(self) -> None:
        def call(_: int):
            return self.service.enable_nodes(["TRAN_CLEAN"])

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(call, range(6)))

        self.assertTrue(all(item["ok"] for item in outcomes))
        self.assertEqual(len(self.service.versions()), 1)
        self.assertEqual(len({row["关系编号"] for row in self._active_lineage()}),
                         len(self._active_lineage()))
        self.assertEqual(len(self._active_lineage()), 4)

    def test_disable_archives_lineage_and_keeps_plan(self) -> None:
        self.assertTrue(self.service.enable_nodes(["TRAN_CLEAN"])["ok"])
        version = self.service.versions()[0]
        plan_snapshot = [dict(item) for item in version["计划"]]
        lineage_ids = [row["id"] for row in self._active_lineage()]

        entry, message, outcome = self.service.run_action(self.clean["id"], "停用编排")
        self.assertIsNotNone(entry, message)
        self.assertTrue(outcome["ok"])

        archived = [row for row in store.rows("lineage") if row["id"] in lineage_ids]
        self.assertTrue(archived)
        archived_clean = [row for row in archived if "TRAN_CLEAN" in row["来源节点"]]
        self.assertTrue(archived_clean)
        self.assertTrue(all(row["生效状态"] == "历史归档" for row in archived_clean))
        untouched = [row for row in archived if "TRAN_CLEAN" not in row["来源节点"]]
        self.assertTrue(all(row["生效状态"] == "生效中" for row in untouched))
        self.assertEqual(version["状态"], "部分停用")
        self.assertEqual(version["计划"], plan_snapshot)
        self.assertEqual(version["停用节点"], ["TRAN_CLEAN"])

        # 历史血缘只读，不能再被状态流转改掉
        lineage = store.find("lineage", archived_clean[0]["id"])
        from app.services.lineage import LineageService
        blocked, reason = LineageService().run_action(lineage["id"], "废弃关系")
        self.assertIsNone(blocked)
        self.assertIn("只读追溯", reason)

    def test_commit_failure_rolls_back_everything(self) -> None:
        before_status = self.clean["status"]
        before_lineage = len(store.rows("lineage"))
        before_audit = len(store.rows("auditlog"))

        with mock.patch.object(
            transform_service, "_append_audit", side_effect=RuntimeError("审计写入失败")
        ):
            with self.assertRaises(RuntimeError):
                self.service.enable_nodes(["TRAN_CLEAN"])

        self.assertEqual(self.clean["status"], before_status)
        self.assertEqual(len(store.rows("lineage")), before_lineage)
        self.assertEqual(len(store.rows("auditlog")), before_audit)
        self.assertEqual(self.service.versions(), [])


if __name__ == "__main__":
    unittest.main()
