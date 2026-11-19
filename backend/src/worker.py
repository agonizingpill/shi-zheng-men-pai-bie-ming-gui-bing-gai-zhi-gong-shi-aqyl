import os,time,json,pathlib,logging
import redis
from sqlalchemy import select,update,delete
from .database import Session
from .models import MatchRun,Candidate
from .matching import match
q=redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
QUEUE='address:matching'
def push(run_id,attempt):q.rpush(QUEUE,json.dumps({'id':run_id,'attempt':attempt}))
def dispatch(s,run):
 run.attempt+=1;run.state='queued';run.error='';s.flush();return run.attempt
def recover():
 with Session() as s:
  stuck=s.scalars(select(MatchRun).where(MatchRun.state.in_(['queued','computing'])).order_by(MatchRun.id)).all()
  pending=[(x.id,dispatch(s,x)) for x in stuck]
  s.commit()
 for run_id,attempt in pending:push(run_id,attempt)
 if pending:logging.info('恢复%d个中断的匹配批次：%s',len(pending),[x[0] for x in pending])
def claim(run_id,attempt):
 with Session() as s:
  r=s.execute(update(MatchRun).where(MatchRun.id==run_id,MatchRun.attempt==attempt,MatchRun.state=='queued').values(state='computing'));s.commit();return r.rowcount==1
def finish(run_id,attempt,**values):
 with Session() as s:
  r=s.execute(update(MatchRun).where(MatchRun.id==run_id,MatchRun.attempt==attempt,MatchRun.state=='computing').values(**values))
  if r.rowcount!=1:s.rollback();logging.warning('批次%s第%s次计算结果已过期，丢弃',run_id,attempt);return False
  if values.get('state')=='ready':
   s.execute(delete(Candidate).where(Candidate.run_id==run_id))
   for d in values['result']['candidates']:s.add(Candidate(run_id=run_id,**d))
  s.commit();return True
def process(msg):
 run_id,attempt=int(msg['id']),int(msg['attempt'])
 if not claim(run_id,attempt):return
 with Session() as s:snap=(s.get(MatchRun,run_id).snapshot or {})
 try:
  if not snap:raise ValueError('本批缺少登记快照，请发起新批')
  result=match(snap.get('entrances',[]),snap.get('aliases',[]))
  finish(run_id,attempt,state='ready',result=result,error='')
 except Exception as e:
  logging.exception('matching failed');finish(run_id,attempt,state='failed',error=str(e)[:500])
def main():
 logging.basicConfig(level=logging.INFO)
 recover()
 while True:
  pathlib.Path('/artifacts/address-heartbeat').write_text(str(time.time()))
  item=q.blpop(QUEUE,timeout=2)
  if not item:continue
  try:msg=json.loads(item[1])
  except ValueError:logging.warning('无法识别的队列消息：%r',item[1]);continue
  try:process(msg)
  except Exception:logging.exception('批次处理异常，留待恢复')
if __name__=='__main__':main()
