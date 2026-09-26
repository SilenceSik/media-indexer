# -*- coding: utf-8 -*-
"""改档位门槛后，**存量库**的 `titles.tier` 必须跟着重算。

为什么单独立一个测试文件：这是唯一一处「代码改了、但已落库的数据要跟着变」
的地方。门槛本身有 test_magnet_judge 覆盖，这里只测**数据迁移**这一层 ——
不重算的话，界面会显示旧档位、删除门控会按旧档放行/拦截，而且**不会报错**，
属于最难发现的那类 bug。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database_v2 import (                                # noqa: E402
    TIER_RULE_VERSION,
    Database,
)
from core.magnet_judge import tier_of                         # noqa: E402


def seed(path, rows, version=None):
    """建库并塞入 (number, 正确磁力, 评论数, 落库档位) 四元组。"""

    db = Database(str(path))

    for number, correct, comments, tier in rows:

        db.conn.execute(
            "INSERT INTO titles (number, correct_magnets, comments_count, tier) "
            "VALUES (?, ?, ?, ?)",
            (number, correct, comments, tier),
        )

    db.conn.commit()

    if version is not None:
        db.conn.execute("PRAGMA user_version = {}".format(version))
        db.conn.commit()

    return db


def tiers(path):
    c = Database(str(path))                 # 每次重开都会跑 migrate()

    return {
        r["number"]: r["tier"]
        for r in c.conn.execute("SELECT number, tier FROM titles").fetchall()
    }


def test_stale_tiers_are_recomputed(tmp_path):
    """库里的档位是**旧门槛**算的（>=3 即「高」）——重开必须按新门槛改掉。"""

    p = tmp_path / "lib.db"

    seed(p, [
        # 旧口径下 3 条就是「高」，新口径要 > 5
        ("OLD-HIGH", 3, 0, "高"),
        # 旧口径极高要评论 >= 50；新口径磁力 > 3 且评论 > 10
        ("OLD-TOP", 5, 12, "高"),
        # 新口径下仍然该是「高」
        ("STAY-HIGH", 9, 0, "高"),
    ], version=1)

    got = tiers(p)

    assert got == {
        "OLD-HIGH": tier_of(3, 0),          # -> 低
        "OLD-TOP": tier_of(5, 12),          # -> 极高
        "STAY-HIGH": "高",
    }
    assert got["OLD-HIGH"] == "低"
    assert got["OLD-TOP"] == "极高"


def test_migration_runs_once_and_is_idempotent(tmp_path):
    """跑第二遍不该再改任何东西 —— 且不能把 version 又写回去。"""

    p = tmp_path / "lib.db"

    seed(p, [("A-001", 3, 0, "高")], version=1)

    first = tiers(p)
    second = tiers(p)

    assert first == second == {"A-001": "低"}

    c = Database(str(p))
    v = c.conn.execute("PRAGMA user_version").fetchone()[0]

    assert v == TIER_RULE_VERSION


def test_manual_edit_is_not_silently_reverted(tmp_path):
    """版本已是最新时，重开**不该**再动档位。

    否则任何手工修正都会被下一次开库覆盖 —— 迁移只该在版本落后时跑。
    """

    p = tmp_path / "lib.db"

    seed(p, [("A-001", 9, 0, "高")], version=TIER_RULE_VERSION)

    db = Database(str(p))

    db.conn.execute("UPDATE titles SET tier = '低' WHERE number = 'A-001'")
    db.conn.commit()

    assert tiers(p)["A-001"] == "低", "版本已最新，不该被迁移改回去"


def test_brand_new_db_gets_current_version(tmp_path):
    """新库也要落到当前版本，否则每次开库都白跑一遍全表重算。"""

    p = tmp_path / "lib.db"

    Database(str(p))

    c = Database(str(p))

    assert c.conn.execute("PRAGMA user_version").fetchone()[0] == \
        TIER_RULE_VERSION


def test_version_constant_is_documented():
    """改门槛忘了 +1，等于存量库静默错档 —— 至少保证常量有说明和版本史。"""

    from core import database_v2

    doc = database_v2.__doc__ or ""
    src = open(database_v2.__file__, encoding="utf-8").read()

    assert TIER_RULE_VERSION >= 1

    # 版本史必须写清楚每一版的门槛，否则下一个人不知道该不该 +1
    assert "版本历史" in src
    assert src.count("TIER_RULE_VERSION") >= 3

    assert doc is not None
