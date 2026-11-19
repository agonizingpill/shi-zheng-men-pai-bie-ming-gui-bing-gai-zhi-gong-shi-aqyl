#!/usr/bin/env python3
"""gb-1036 批次化别名匹配后端场景测试。

覆盖需求：
  1. 发起时定下本批要算的入口和别名（快照冻结）
  2. 等候和计算期间照常登记
  3. 计算中断后能恢复原批（recover / resume）
  4. 补登资料留给下次计算
  5. 重试不重复生成候选
  6. 晚回来的旧尝试不能盖掉已算完的结果
  7. 候选与状态同事务落库（候选齐了才是 ready）

运行：/tmp/venv-gb1036/bin/python /workspace/test-artifacts/gb-1036/run_backend_tests.py
使用 SQLite + 内存假 Redis，不触碰生产 MySQL/Redis。
"""
import os, sys, json, types, hashlib, tempfile

TMP = tempfile.mkdtemp(prefix='gb1036-')
os.environ['DATABASE_URL'] = 'sqlite:///' + TMP + '/test.db'
os.environ['REDIS_URL'] = 'redis://fake/0'


class FakeRedis:
    def __init__(self):
        self.lists = {}
        self.broken = False

    def rpush(self, k, v):
        if self.broken:
            raise ConnectionError('redis down')
        self.lists.setdefault(k, []).append(v)
        return len(self.lists[k])

    def blpop(self, k, timeout=0):
        lst = self.lists.get(k, [])
        return (k, lst.pop(0)) if lst else None

    def llen(self, k):
        return len(self.lists.get(k, []))

    def ping(self):
        return True


fake = FakeRedis()
redis_stub = types.ModuleType('redis')
redis_stub.from_url = lambda *a, **k: fake
sys.modules['redis'] = redis_stub

import sqlalchemy
_orig_create_engine = sqlalchemy.create_engine


def patched_create_engine(url, **kw):
    if url.startswith('sqlite'):
        kw.pop('isolation_level', None)
    return _orig_create_engine(url, **kw)


sqlalchemy.create_engine = patched_create_engine

sys.path.insert(0, '/workspace/backend')
from scripts.seed_demo import seed
seed()

import fastapi
from sqlalchemy import select
from src.database import Session
from src.models import MatchRun, Candidate, Audit
from src.schemas import EntranceIn, AliasIn
from src.common import dump
from src import routes, worker
from src.matching import match

OP = {'email': 'operator@changqiao.local', 'role': 'operator'}
VIEWER = {'email': 'viewer@changqiao.local', 'role': 'viewer'}
QUEUE = 'address:matching'
passed = []


def check(name, cond):
    assert cond, 'FAILED: ' + name
    passed.append(name)
    print('ok -', name)


def run_row(i):
    with Session() as s:
        return dump(s.get(MatchRun, i))


def cands(i):
    with Session() as s:
        return [dump(c) for c in s.scalars(select(Candidate).where(Candidate.run_id == i)).all()]


def pop_msg():
    item = fake.blpop(QUEUE)
    assert item, 'queue empty'
    return json.loads(item[1])


# ---------- 1. 发起时锁定本批入口与别名 ----------
d1 = routes.matching(u=OP)
check('批次1创建为queued/第1次计算', d1['state'] == 'queued' and d1['attempt'] == 1)
check('批次1快照含4入口4别名', len(d1['snapshot']['entrances']) == 4 and len(d1['snapshot']['aliases']) == 4)
m1 = pop_msg()
check('队列消息携带批次与尝试号', m1 == {'id': d1['id'], 'attempt': 1})

# ---------- 2. 等候期间照常登记；补登不进本批快照 ----------
e = routes.ecreate(EntranceIn(code='CQ-099', street='长桥路', number=99, label='长桥路99号东门', lat=31.2, lng=121.4), u=OP)
a = routes.aliases(AliasIn(entrance_id=e['id'], text='长桥路九十九号旧门'), u=OP)
r1 = run_row(d1['id'])
check('等候期间登记成功且快照保持4别名', r1['state'] == 'queued' and len(r1['snapshot']['aliases']) == 4)

# ---------- 3. worker按快照计算；补登入口不进候选 ----------
worker.process(m1)
r1 = run_row(d1['id'])
c1 = cands(d1['id'])
check('批次1算完', r1['state'] == 'ready')
check('批次1有候选且候选数与结果一致（候选齐了才ready）', len(c1) > 0 and len(c1) == len(r1['result']['candidates']))
ids_in_c = {x['source_id'] for x in c1} | {x['target_id'] for x in c1}
check('补登入口不出现在本批候选', e['id'] not in ids_in_c)
expect_sha = hashlib.sha256(json.dumps([r1['snapshot']['entrances'], r1['snapshot']['aliases']], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
check('结果SHA256覆盖本批快照', r1['result']['source_sha256'] == expect_sha)
check('簇标签只含本批别名', {x['alias_id'] for x in r1['result']['clusters']} == {x['id'] for x in r1['snapshot']['aliases']})

# ---------- 4. 重试不重复生成候选 ----------
worker.process(m1)
check('重复投递同一条消息候选不翻倍', len(cands(d1['id'])) == len(c1))

# ---------- 5. 中断后恢复原批；补登资料留给下次计算 ----------
d2 = routes.matching(u=OP)
check('批次2快照含补登后的5入口5别名', len(d2['snapshot']['entrances']) == 5 and len(d2['snapshot']['aliases']) == 5)
m2 = pop_msg()
check('worker领取批次2', worker.claim(d2['id'], m2['attempt']) is True)
check('批次2进入computing', run_row(d2['id'])['state'] == 'computing')
# 计算期间照常登记
e2 = routes.ecreate(EntranceIn(code='CQ-100', street='长桥路', number=100, label='长桥路100号侧门', lat=31.2, lng=121.4), u=OP)
a2 = routes.aliases(AliasIn(entrance_id=e2['id'], text='长桥路一百号旧称'), u=OP)
check('计算期间照常登记', e2['id'] and a2['id'])
# 模拟worker被杀：不写回，直接重启恢复
worker.recover()
r2 = run_row(d2['id'])
check('恢复后批次2回到queued且尝试号+1', r2['state'] == 'queued' and r2['attempt'] == m2['attempt'] + 1)
m2b = pop_msg()
check('恢复消息携带新尝试号', m2b == {'id': d2['id'], 'attempt': m2['attempt'] + 1})
worker.process(m2)   # 中断前的旧消息晚到
check('旧尝试消息被跳过，批次仍在排队', run_row(d2['id'])['state'] == 'queued' and cands(d2['id']) == [])
worker.process(m2b)  # 新尝试算完
r2 = run_row(d2['id'])
c2 = cands(d2['id'])
check('批次2恢复后算完', r2['state'] == 'ready')
check('恢复重试候选不重复', len(c2) == len({(x['source_id'], x['target_id']) for x in c2}) == len(r2['result']['candidates']))
check('批次2候选仍不含计算期间补登的入口', e2['id'] not in ({x['source_id'] for x in c2} | {x['target_id'] for x in c2}))

# ---------- 6. 晚回来的旧尝试不能盖掉已算完的结果 ----------
d3 = routes.matching(u=OP)
pop_msg()
check('批次3被领取', worker.claim(d3['id'], d3['attempt']) is True)
d3r = routes.resume(d3['id'], u=OP)
check('手动恢复批次3：queued且尝试号+1', d3r['state'] == 'queued' and d3r['attempt'] == d3['attempt'] + 1)
m3 = pop_msg()
worker.process(m3)
r3 = run_row(d3['id'])
check('批次3经恢复算完', r3['state'] == 'ready')
late = worker.finish(d3['id'], d3['attempt'], state='ready', result={'candidates': [], 'tampered': True}, error='')
check('旧尝试写回被拒绝', late is False)
r3b = run_row(d3['id'])
check('已算完结果未被旧尝试覆盖', r3b['result'] == r3['result'] and len(cands(d3['id'])) == len(r3['result']['candidates']))
check('旧尝试无法再领取', worker.claim(d3['id'], d3['attempt']) is False)

# ---------- 7. 恢复守卫 ----------
try:
    routes.resume(d3['id'], u=OP)
    check('已算完批次拒绝恢复', False)
except fastapi.HTTPException as ex:
    check('已算完批次拒绝恢复', ex.status_code == 409)

with Session() as s:
    tiny = MatchRun(state='queued', attempt=1, snapshot={'entrances': [{'id': 1, 'street': '长桥路', 'number': 18}], 'aliases': [{'id': 1, 'entrance_id': 1, 'text': '长桥路18号'}]})
    s.add(tiny)
    s.commit()
    tid = tiny.id
worker.process({'id': tid, 'attempt': 1})
check('文本不足真实failed', run_row(tid)['state'] == 'failed' and '至少两个' in run_row(tid)['error'])
try:
    routes.resume(tid, u=OP)
    check('失败批次拒绝恢复', False)
except fastapi.HTTPException as ex:
    check('失败批次拒绝恢复', ex.status_code == 409)

with Session() as s:
    nosnap = MatchRun(state='queued', attempt=1)
    s.add(nosnap)
    s.commit()
    nid = nosnap.id
worker.process({'id': nid, 'attempt': 1})
check('无快照批次明确失败', run_row(nid)['state'] == 'failed' and '快照' in run_row(nid)['error'])

# ---------- 8. 恢复时队列不可达：状态回退仍可再次恢复 ----------
d4 = routes.matching(u=OP)
pop_msg()
check('批次4被领取', worker.claim(d4['id'], d4['attempt']) is True)
fake.broken = True
try:
    routes.resume(d4['id'], u=OP)
    check('队列不可达恢复报错', False)
except fastapi.HTTPException as ex:
    check('队列不可达恢复报错', ex.status_code == 409)
r4 = run_row(d4['id'])
check('恢复失败后状态回退为computing', r4['state'] == 'computing')
fake.broken = False
d4r = routes.resume(d4['id'], u=OP)
check('队列恢复后再次恢复成功', d4r['state'] == 'queued' and pop_msg()['id'] == d4['id'])

# ---------- 9. 约束与权限 ----------
cols = {tuple(c.columns.keys()) for c in Candidate.__table__.constraints if isinstance(c, sqlalchemy.UniqueConstraint)}
check('候选唯一约束(run_id,source_id,target_id)', ('run_id', 'source_id', 'target_id') in cols)
try:
    routes.matching(u=VIEWER)
    check('viewer禁止发起匹配', False)
except fastapi.HTTPException as ex:
    check('viewer禁止发起匹配', ex.status_code == 403)
with Session() as s:
    actions = [x.action for x in s.scalars(select(Audit)).all()]
check('恢复动作留审计', '恢复匹配批次' in actions and '匹配别名' in actions)

from src.main import app
check('FastAPI应用导入正常', any(getattr(r, 'path', '') == '/matches/{id}/resume' for r in app.routes))

print('\nPASS %d 项后端场景检查' % len(passed))
