// gb-1036 归并核验页前端渲染测试
// 运行：node --experimental-default-type=module /workspace/test-artifacts/gb-1036/run_frontend_tests.mjs
const proposals=await import('file:///workspace/frontend/src/pages/proposals.js');
const constants=await import('file:///workspace/frontend/src/constants.js');
const{render}=proposals,{STATES}=constants;
let n=0,failed=0;
const ok=(name,cond)=>{if(cond){n++;console.log('ok -',name)}else{failed++;console.error('FAIL -',name);process.exitCode=1}};
const base={
 entrances:[
  {id:1,code:'CQ-018-A',street:'长桥路',number:18,label:'长桥路18号正门',lat:31.2,lng:121.4},
  {id:2,code:'CQ-018-B',street:'长桥路',number:18,label:'长桥路十八号旧门牌',lat:31.2,lng:121.4}],
 aliases:[{id:1,entrance_id:1,text:'长桥路18号正门',state:'verified'},{id:2,entrance_id:2,text:'长桥路十八号旧门牌',state:'verified'}],
 evidence:[],proposals:[],objections:[],notices:[],redirects:[],
 runs:[{id:1,state:'ready',attempt:2,error:'',
  snapshot:{entrances:[{id:1,code:'CQ-018-A'},{id:2,code:'CQ-018-B'}],
            aliases:[{id:1,entrance_id:1,text:'长桥路18号正门'},{id:2,entrance_id:2,text:'长桥路十八号旧门牌'}]},
  result:{algorithm:'x',source_sha256:'abc',clusters:[],
   evaluation:{seed:42,train_pairs:36,holdout_pairs:12,train_ids:[],holdout_ids:[],precision:1,recall:1,threshold:.55,label_source:'模拟'},
   candidates:[{source_id:1,target_id:2,similarity:.9,conflict:0}]}}],
 candidates:[{id:1,run_id:1,source_id:1,target_id:2,similarity:.9,conflict:0}]};
const withRun=patch=>({...base,runs:[{...base.runs[0],...patch}]});
// 状态文案
ok('queued显示排队中',STATES.queued==='排队中');
ok('computing显示计算中',STATES.computing==='计算中');
// 候选齐了才显示算完
let html=render(base);
ok('候选齐了显示已计算',html.includes('已计算')&&!html.includes('>计算中<'));
ok('名册列出本批用过的别名',html.includes('长桥路十八号旧门牌')&&html.includes('CQ-018-A'));
ok('名册显示锁定规模',html.includes('本批锁定 2 入口 / 2 别名'));
ok('显示第几次计算',html.includes('第 2 次计算'));
let incomplete=render({...base,candidates:[]});
ok('候选未齐不显示已计算',!incomplete.includes('已计算')&&incomplete.includes('计算中'));
// 中断批次出现恢复按钮
let queued=render(withRun({state:'queued',result:null}));
ok('排队中批次可恢复',queued.includes('/matches/1/resume')&&queued.includes('排队中'));
let computing=render(withRun({state:'computing',result:null}));
ok('计算中批次可恢复',computing.includes('/matches/1/resume')&&computing.includes('计算中'));
// 算完/失败不再给恢复按钮
ok('已计算批次无恢复按钮',!html.includes('/matches/1/resume'));
let failedHtml=render(withRun({state:'failed',result:null,error:'至少两个入口和两个有文字的别名'}));
ok('失败批次无恢复按钮且显示错误',!failedHtml.includes('/matches/1/resume')&&failedHtml.includes('至少两个入口和两个有文字的别名'));
// 空批次
ok('无批次时名册兜底',render({...base,runs:[],candidates:[]}).includes('尚无匹配批次'));
console.log(`\nPASS ${n} 项前端检查`+(failed?`，FAIL ${failed} 项`:''));
