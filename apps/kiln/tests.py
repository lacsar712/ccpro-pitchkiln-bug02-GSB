import threading

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from .forms import FireHearthForm
from .models import FireHearth
from .views import _board_context

User = get_user_model()


def _hearth_payload(tag, lane="5", resinGrade="特级脂", phase="cold"):
    return {
        "lane": lane,
        "tag": tag,
        "resinGrade": resinGrade,
        "phase": phase,
    }


class HearthCreateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tester", password="x")
        self.client.force_login(self.user)

    def test_duplicate_tag_is_rejected_not_renamed(self):
        FireHearth.objects.create(lane=1, tag="甲号牌", resinGrade="x")
        resp = self.client.post(
            reverse("hearth_create"), _hearth_payload("甲号牌"), follow=False
        )
        # 拒绝 → 留在表单页（200）并给出字段错误，而不是 302 成功跳转
        self.assertEqual(resp.status_code, 200)
        self.assertIn("灶牌已存在", resp.context["form"].errors["tag"][0])
        self.assertEqual(FireHearth.objects.filter(tag="甲号牌").count(), 1)
        # 绝不允许以「-复」后缀伪装唯一另存
        self.assertFalse(FireHearth.objects.filter(tag__endswith="-复").exists())

    def test_db_constraint_blocks_bare_duplicate_insert(self):
        FireHearth.objects.create(lane=1, tag="甲号牌", resinGrade="x")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FireHearth.objects.create(lane=2, tag="甲号牌", resinGrade="y")


class HearthUpdateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tester", password="x")
        self.client.force_login(self.user)
        self.a = FireHearth.objects.create(lane=1, tag="甲号牌", resinGrade="x")
        self.b = FireHearth.objects.create(lane=2, tag="乙号牌", resinGrade="y")

    def test_update_into_existing_tag_is_rejected(self):
        resp = self.client.post(
            reverse("hearth_update", args=[self.b.pk]),
            _hearth_payload("甲号牌", lane="2"),
            follow=False,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("灶牌已存在", resp.context["form"].errors["tag"][0])
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        # 两条记录原样：被编辑灶未被强改成撞名灶
        self.assertEqual(self.a.tag, "甲号牌")
        self.assertEqual(self.b.tag, "乙号牌")
        self.assertEqual(FireHearth.objects.filter(tag="甲号牌").count(), 1)

    def test_update_keeping_own_tag_is_allowed(self):
        resp = self.client.post(
            reverse("hearth_update", args=[self.a.pk]),
            _hearth_payload("甲号牌", lane="7", resinGrade="新品级"),
            follow=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.a.refresh_from_db()
        self.assertEqual(self.a.lane, 7)
        self.assertEqual(self.a.resinGrade, "新品级")

    def test_form_excludes_self_when_checking_uniqueness(self):
        form = FireHearthForm(_hearth_payload("甲号牌", lane="1"), instance=self.a)
        self.assertTrue(form.is_valid(), form.errors)


class ConcurrentCreateTests(TransactionTestCase):
    """两人同时提交同一灶牌：只许一笔入库，另一笔被拒绝。"""

    def setUp(self):
        self.user = User.objects.create_superuser("tester", "t@x", "pw")

    def _worker(self, barrier, outcomes, errors):
        client = Client()
        # 子线程独立连接：先配写锁等待，再走登录与提交
        with connections["default"].cursor() as cur:
            cur.execute("PRAGMA busy_timeout = 20000")
        try:
            client.force_login(self.user)
            barrier.wait(timeout=15)
            resp = client.post(
                reverse("hearth_create"), _hearth_payload("并发牌", lane="8")
            )
            outcomes.append(resp.status_code)
        except Exception as exc:  # noqa: BLE001 - 回传主线程断言
            errors.append(repr(exc))
        finally:
            connections.close_all()

    def test_only_one_concurrent_insert_wins(self):
        self.assertEqual(connections["default"].vendor, "sqlite")
        with connection.cursor() as cur:
            cur.execute("PRAGMA busy_timeout = 20000")

        barrier = threading.Barrier(2)
        outcomes, errors = [], []
        threads = [
            threading.Thread(
                target=self._worker, args=(barrier, outcomes, errors)
            )
            for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=45)
            self.assertFalse(t.is_alive(), "并发请求线程超时未结束")

        self.assertEqual(errors, [])
        self.assertEqual(sorted(outcomes), [200, 302])
        self.assertEqual(FireHearth.objects.filter(tag="并发牌").count(), 1)
        self.assertFalse(FireHearth.objects.filter(tag__endswith="-复").exists())


class BoardRecoveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tester", password="x")
        self.client.force_login(self.user)

    def test_board_opens_after_failed_save(self):
        FireHearth.objects.create(lane=1, tag="甲号牌", resinGrade="x", phase="cold")
        fail = self.client.post(reverse("hearth_create"), _hearth_payload("甲号牌"))
        self.assertEqual(fail.status_code, 200)
        board = self.client.get(reverse("home"))
        self.assertEqual(board.status_code, 200)

    def test_board_recomputes_with_duplicate_dirty_tags(self):
        """历史脏数据（重牌）不得再让看板崩溃：瓦片全显示、图例可复算。"""
        dup1 = FireHearth(lane=4, tag="重名脏牌", resinGrade="x", phase="cold")
        dup2 = FireHearth(lane=5, tag="重名脏牌", resinGrade="y", phase="holding")

        ctx = _board_context([dup1, dup2])  # 旧实现在此必抛 KeyError
        self.assertEqual(len(ctx["hearths"]), 2)
        lanes = dict(ctx["lanes"])
        self.assertEqual(lanes[4], [dup1])
        self.assertEqual(lanes[5], [dup2])
        counts = dict((k, c) for k, _label, c in ctx["phase_legend"])
        self.assertEqual(counts["cold"], 1)
        self.assertEqual(counts["holding"], 1)

    def test_grid_partial_renders_duplicate_dirty_tags(self):
        """端到端：查询层若返回历史重牌，整页与网格局部都正常渲染。"""
        from unittest.mock import patch

        dup1 = FireHearth.objects.create(
            lane=4, tag="脏牌-甲", resinGrade="x", phase="cold"
        )
        dup2 = FireHearth.objects.create(
            lane=5, tag="脏牌-乙", resinGrade="y", phase="holding"
        )
        # 库里仍唯一，仅在内存里模拟查询层返回的历史重牌脏数据
        dup1.tag = dup2.tag = "重名脏牌"
        with patch(
            "apps.kiln.views._hearths_for_board", return_value=[dup1, dup2]
        ):
            board = self.client.get(reverse("home"))
            self.assertEqual(board.status_code, 200)
            partial = self.client.get(reverse("floor_grid"))
            self.assertEqual(partial.status_code, 200)
            text = partial.content.decode()
            # 两块瓦片都渲染，旧实现早在视图层就 KeyError 500
            self.assertEqual(text.count("重名脏牌"), 2)
            counts = dict((k, c) for k, _l, c in board.context["phase_legend"])
            self.assertEqual(counts["cold"], 1)
            self.assertEqual(counts["holding"], 1)


class MigrationDedupTests(TransactionTestCase):
    """存量重牌在加唯一约束的迁移中被确定性改名去重。

    必须用 TransactionTestCase：SQLite 不允许在原子事务中开关
    foreign_keys，而迁移的 schema editor 要求外层无包裹事务。
    """

    migrate_from = ("kiln", "0002_firehearth_tag_drop_unique")
    migrate_to = ("kiln", "0003_firehearth_tag_unique")

    @property
    def executor(self):
        return MigrationExecutor(connection)

    def migrate(self, target):
        self.executor.migrate(target)
        return self.executor.loader.project_state(target).apps

    def setUp(self):
        super().setUp()
        apps = self.migrate([self.migrate_from])
        Hearth = apps.get_model("kiln", "FireHearth")
        Hearth.objects.create(lane=1, tag="坑火-西二", resinGrade="a")
        Hearth.objects.create(lane=2, tag="坑火-西二", resinGrade="b")
        Hearth.objects.create(lane=3, tag="坑火-西二", resinGrade="c")

    def tearDown(self):
        # 回滚到 kiln zero 再迁到最新，供后续用例与测试清理使用
        self.executor.migrate([("kiln", None)])
        self.executor.migrate([self.migrate_to])
        super().tearDown()

    def test_duplicates_renamed_before_unique_constraint(self):
        apps = self.migrate([self.migrate_to])
        Hearth = apps.get_model("kiln", "FireHearth")
        tags = list(Hearth.objects.order_by("id").values_list("tag", flat=True))
        self.assertEqual(len(tags), len(set(tags)))
        # 最小 id 保留原名，其余带自身 id 后缀
        self.assertEqual(tags[0], "坑火-西二")
        self.assertRegex(tags[1], r"^坑火-西二-重\d+$")
        self.assertRegex(tags[2], r"^坑火-西二-重\d+$")

    def test_long_tags_are_truncated_to_fit(self):
        apps = self.migrate([self.migrate_from])
        Hearth = apps.get_model("kiln", "FireHearth")
        long_tag = "长" * 40
        Hearth.objects.create(lane=9, tag=long_tag, resinGrade="z")
        Hearth.objects.create(lane=10, tag=long_tag, resinGrade="zz")
        apps = self.migrate([self.migrate_to])
        Hearth = apps.get_model("kiln", "FireHearth")
        tags = list(Hearth.objects.values_list("tag", flat=True))
        for tag in tags:
            self.assertLessEqual(len(tag), 40)
        self.assertEqual(len(tags), len(set(tags)))
        self.assertEqual(Hearth.objects.count(), 5)
