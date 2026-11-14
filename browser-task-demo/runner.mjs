import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync, renameSync, existsSync, openSync, closeSync, unlinkSync } from 'node:fs';
import { dirname } from 'node:path';

export const fingerprint = job => createHash('sha256').update(JSON.stringify({id:job.id,title:job.title,body:job.body})).digest('hex');
export class MemoryStore {
  records = new Map(); busy = false;
  get(id) { return this.records.get(id); }
  set(id, value) { this.records.set(id, structuredClone(value)); }
  acquire() { if(this.busy)return false;this.busy=true;return true; }
  release() { this.busy=false; }
}
export class FileStore extends MemoryStore {
  constructor(path) { super();this.path=path;this.lockPath=path+'.lock';this.refresh(); }
  refresh(){this.records=existsSync(this.path)?new Map(Object.entries(JSON.parse(readFileSync(this.path,'utf8')))):new Map();}
  get(id){this.refresh();return super.get(id);}
  acquire(){if(this.busy)return false;mkdirSync(dirname(this.path),{recursive:true});try{closeSync(openSync(this.lockPath,'wx'));this.busy=true;return true;}catch(error){if(error.code==='EEXIST')return false;throw error;}}
  release(){if(this.busy){unlinkSync(this.lockPath);this.busy=false;}}
  set(id,value) { super.set(id,value);mkdirSync(dirname(this.path),{recursive:true});const temp=this.path+'.tmp';writeFileSync(temp,JSON.stringify(Object.fromEntries(this.records),null,2)+'\n');renameSync(temp,this.path); }
}

// The port must use normal, visible UI controls. It must reject an unexpected
// origin/page before acting. It does not accept selectors or code from a job.
export async function runJob({ui,store,job,approval,signal,onAudit=()=>{}}) {
  if(!job||!/^[A-Za-z0-9_-]{1,40}$/.test(job.id)||typeof job.title!=='string'||!job.title.trim()||job.title.length>60||typeof job.body!=='string'||!job.body.trim()||job.body.length>1000)throw new Error('INVALID_JOB');
  const hash=fingerprint(job), prior=store.get(job.id);
  if(prior){
    if(prior.hash!==hash)return {status:'conflict',id:job.id};
    if(prior.status==='succeeded')return {...prior,replayed:true};
    return {...prior,requiresHuman:true};
  }
  if(!approval||approval.contentHash!==hash||approval.approved!==true)return {status:'awaiting_approval',id:job.id};
  if(!store.acquire())return {status:'busy',id:job.id};
  let stage='start', submitted=false;
  const audit=[];
  const event=(state,detail)=>{const e={state,detail,at:new Date().toISOString()};audit.push(e);onAudit(e);};
  const guard=async()=>{if(signal?.aborted)throw new Error('CANCELLED');await ui.assertScope();};
  const state=value=>store.set(job.id,{id:job.id,hash,stage,submitted,audit:[...audit],...value});
  try {
    // Re-read only after holding the worker lock, avoiding a stale pre-lock read.
    const late=store.get(job.id);
    if(late){if(late.hash!==hash)return {status:'conflict',id:job.id};return late.status==='succeeded'?{...late,replayed:true}:{...late,requiresHuman:true};}
    state({status:'running'});event('running','approved content accepted');
    await guard();stage='fill';await ui.fill(job);
    await guard();stage='preview';await ui.preview();
    await guard();stage='verify_preview';const p=await ui.readPreview();
    if(p.title!==job.title||p.body!==job.body)throw new Error('PREVIEW_MISMATCH');
    await guard();stage='confirm';await ui.confirm();
    await guard();stage='submit';submitted=true;state({status:'running'});await ui.submit();
    await guard();stage='receipt';const receipt=await ui.readReceipt();
    if(!receipt||receipt.status!=='ok'||receipt.id!==job.id||receipt.title!==job.title||receipt.body!==job.body||receipt.demo!==true)throw new Error(receipt?.code||'RECEIPT_UNCERTAIN');
    event('succeeded',receipt.receipt);state({status:'succeeded',receipt});return store.get(job.id);
  } catch(error) {
    const status=error.message==='CANCELLED'?'cancelled':'needs_human';
    event(status,error.message);state({status,error:error.message,requiresHuman:true});return store.get(job.id);
  } finally { store.release(); }
}

export async function runQueue(options,jobs) {
  const results=[];
  for(const item of jobs){const result=await runJob({...options,...item});results.push(result);if(result.status!=='succeeded')break;}
  return results;
}

export function createPlaywrightPort(page, expectedUrl) {
  const fixed=new URL(expectedUrl);
  if(fixed.protocol!=='https:'&&fixed.hostname!=='127.0.0.1'&&fixed.hostname!=='localhost')throw new Error('UNSAFE_DEMO_URL');
  const title=page.getByRole('textbox',{name:'任务标题',exact:true});
  const body=page.getByRole('textbox',{name:'任务正文',exact:true});
  return {
    async assertScope(){const current=new URL(page.url());if(current.origin!==fixed.origin||current.pathname!==fixed.pathname)throw new Error('UNEXPECTED_PAGE');const scope=await page.locator('[data-testid="scope"]').textContent();if(scope.trim()!=='仅限本演示页面，不连接外部账号。')throw new Error('UNEXPECTED_PAGE');},
    async fill(job){await page.getByRole('textbox',{name:'任务编号',exact:true}).fill(job.id);await title.fill(job.title);await body.fill(job.body);},
    async preview(){await page.getByRole('button',{name:'生成预览',exact:true}).click();},
    async readPreview(){const text=await page.locator('#preview').textContent();const split=text.indexOf('\n\n');return {title:text.slice(0,split),body:text.slice(split+2)};},
    async confirm(){await page.getByRole('checkbox',{name:'我已核对预览内容',exact:true}).check();},
    async submit(){await page.getByRole('button',{name:'执行演示发布',exact:true}).click();},
    async readReceipt(){return JSON.parse(await page.locator('[data-testid="receipt"]').textContent());},
  };
}
