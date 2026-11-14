// Runs only inside an already-authorized official in-app browser session.
// No browser discovery, credentials, cookies or separate automation connection.
export function createIabPort(tab,expectedUrl){
  return {
    async assertScope(){
      const snapshot=await tab.ax.get('state',{disableDiffing:true});
      const match=snapshot.split('\n')[0].match(/URL: "([^"]+)"/);
      if(!match||match[1]!==expectedUrl)throw Error('UNEXPECTED_PAGE');
      if((await tab.playwright.locator('[data-testid="scope"]').textContent()).trim()!=='仅限本演示页面，不连接外部账号。')throw Error('UNEXPECTED_PAGE');
    },
    async fill(job){
      await tab.playwright.getByRole('textbox',{name:'任务编号',exact:true}).fill(job.id);
      await tab.playwright.getByRole('textbox',{name:'任务标题',exact:true}).fill(job.title);
      await tab.playwright.getByRole('textbox',{name:'任务正文',exact:true}).fill(job.body);
    },
    async preview(){await tab.playwright.getByRole('button',{name:'生成预览',exact:true}).click();},
    async readPreview(){const text=await tab.playwright.locator('#preview').textContent(),i=text.indexOf('\n\n');return {title:text.slice(0,i),body:text.slice(i+2)};},
    async confirm(){await tab.playwright.getByRole('checkbox',{name:'我已核对预览内容',exact:true}).check();},
    async submit(){await tab.playwright.getByRole('button',{name:'执行演示发布',exact:true}).click();},
    async readReceipt(){return JSON.parse(await tab.playwright.locator('[data-testid="receipt"]').textContent());},
  };
}
