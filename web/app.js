"use strict";
const $ = (id) => document.getElementById(id);
const L = ["水","甘油溶液","蔗糖溶液"], U = ["U1","U2","U3"], V = [30,70,150,300,500,800];
const tubes = [..."AB"].flatMap(r => Array.from({length:6},(_,i)=>r+(i+1)));
const tips = [..."ABCDEFGH"].flatMap(r => Array.from({length:12},(_,i)=>r+String(i+1).padStart(2,"0")));
const keys = {safe:"横移安全高度",tip_a:"枪头 A01 · 压紧位",tip_col:"枪头 A12 · 孔心",tip_row:"枪头 H01 · 孔心",tube_a:"试管 A1 · 吸液位",tube_col:"试管 A6 · 孔心",tube_row:"试管 B1 · 孔心",receiver:"天平 · 瓶内排液位",waste:"原 C03 · 废头投放位"};
const notes = {safe:"先在现场确认装有最长枪头时也能越过全部容器。",tip_a:"空枪对准 A01，小步下压到枪头装牢，记录最终压紧高度。",tip_col:"抬到横移面，目视对准 A12 中心，只采用 X、Y。",tip_row:"在横移面对准 H01 中心，只采用 X、Y。",tube_a:"装上标定枪头，在 A1 内点动到实际吸液深度，枪头避开管底。",tube_col:"在横移面对准 A6 中心，只采用 X、Y。",tube_row:"在横移面对准 B1 中心，只采用 X、Y。",receiver:"带头对准瓶口，选定排液高度，枪头须离开瓶壁和称盘。",waste:"带头对准废头容器开口，选定可顺利弹出枪头的高度。"};
const pages = {station:["工位设置","⌖"],setup:["课堂配置","▤"],cal:["六点标定","∿"],blind:["盲样判别","◇"],verify:["200 μL 复测","◎"],records:["数据记录","▧"]};
const phaseName = {cal:"六点标定",blind:"盲样",verify:"200 μL"};
const statusName = {running:"执行中",dispensing:"排液中",dispensed:"待退出",waiting_mass:"待补读",measured:"待退头",uncertain:"待核对",void:"已作废",superseded:"已被重做",done:"完成"};
const colours = ["#197651","#ac6a28","#507aa6"];
let S, page, signature="", sending=false, polling=false, timer, flashTimer, chartObserver;
let prefs={ jogstep:"1", calSample:"all", blindSample:"U1", blindVol:"300", blindN:"1", nextTip:"" };
let form={}, guesses={}, lastSession=null;
const esc = x => String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const f = (x,n=2) => x==null||!Number.isFinite(Number(x))?"—":Number(x).toFixed(n);
const val = id => $(id)?.value;
const opted = (v,text,cur) => `<option value="${esc(v)}" ${v===cur?"selected":""}>${esc(text)}</option>`;
const options = (values,cur,placeholder=false) => (placeholder?opted("","请选择",cur):"")+values.map(v=>opted(v,v,cur)).join("");
const numeric = (id,min,max,step,value) => `<input id="${id}" type="number" min="${min}" ${max?'max="'+max+'"':""} step="${step}" value="${esc(value)}">`;
const button = (action,title,kind="",disabled=false,attr="") => `<button data-action="${action}" class="${kind}" ${disabled?"disabled":""} ${attr}>${esc(title)}</button>`;
const panel = (title,body) => `<section class="panel"><h2>${esc(title)}</h2>${body}</section>`;
const head = (title,sub) => `<div class="page-head"><div><h1>${title}</h1><p class="sub">${sub}</p></div></div>`;
const tr = (...cells) => "<tr>"+cells.map(x=>"<td>"+x+"</td>").join("")+"</tr>";
const table = (labels,rows) => `<div class="table-wrap"><table><thead><tr>${labels.map(x=>"<th>"+esc(x)+"</th>").join("")}</tr></thead><tbody>${rows}</tbody></table></div>`;
const setPage = next => { if(!pages[next])return; page=next; location.hash=next; render(true); window.scrollTo(0,0); };
const toast = msg => { $("toast").textContent=msg; $("toast").hidden=false; clearTimeout(flashTimer); flashTimer=setTimeout(()=>$("toast").hidden=true,6000); };
const confirmAction = (title,msg) => new Promise(resolve=>{
    const d=$("dialog"); $("dialog-title").textContent=title; $("dialog-text").textContent=msg;
    const finish=result=>{ d.close(); $("dialog-yes").onclick=$("dialog-no").onclick=d.oncancel=null; resolve(result); };
    $("dialog-yes").onclick=()=>finish(true); $("dialog-no").onclick=()=>finish(false);
    d.oncancel=e=>{e.preventDefault();finish(false);}; d.showModal();
});
async function post(path,data){
    const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Workshop-Key":document.querySelector('meta[name="workshop-key"]').content},body:JSON.stringify(data)});
    const out=await r.json(); if(!r.ok)throw Error(out.error||"请求未完成"); return out;
}
async function send(op,data={}){
    if(sending||S.busy)return;
    sending=true; render(true);
    try { await post("/api/action",{session:S.session?.id,...data,op,request:crypto.randomUUID()}); }
    catch(e){toast(e.message);}
    finally{sending=false;await poll(true);}
}
async function poll(force=false){
    if(polling){if(force)setTimeout(()=>poll(true),100);return;} polling=true;
    try{
        const r=await fetch("/api/state"); if(!r.ok)throw Error("本地服务读取失败");
        const next=await r.json(); const old=S;
        S=next;
        if(old && old.tips!==S.tips)prefs.nextTip="";
        if(!page)page=pages[location.hash.slice(1)]?location.hash.slice(1):"station";
        if(lastSession!==null && S.session?.id!==lastSession){form={};guesses={};}
        if(old&&S.session&&old.session?.id!==S.session.id&&page==="setup"){form={};guesses={};page="cal";location.hash=page;}
        lastSession=S.session?.id??0;
        render(force);
    }catch(e){$("feedback").innerHTML='<div class="fail">本地工站未连接。请打开“启动实操.cmd”，然后刷新。</div>';}
    finally{polling=false;}
}
function readDraft(){
    document.querySelectorAll("[data-draft]").forEach(el=>form[el.id]=el.value);
    document.querySelectorAll("[data-pref]").forEach(el=>prefs[el.id]=el.value);
    document.querySelectorAll("[data-guess]").forEach(el=>guesses[el.dataset.guess]=el.value);
}
function render(force=false){
    if(!S)return;
    const sig=JSON.stringify([page,S,sending]);
    if(!force&&sig===signature)return;
    signature=sig;
    $("nav").innerHTML=Object.entries(pages).map(([k,[title,icon]])=>`<button data-nav="${k}" class="${k===page?"active":""}"><span class="navmark" aria-hidden="true">${icon}</span>${title}</button>`).join("");
    $("crumb").textContent=(S.session?.name||"本地实操工作台")+" / "+pages[page][0];
    $("link-status").innerHTML=`<span class="${S.connected?"online":"offline"}"><i></i> ${S.connected?"真机已连接":"设备未连接"}</span>`;
    $("stop").disabled=!S.connected&&!S.busy;
    const p=S.progress;
    let feed=`<div class="live ${S.busy?"busy":""}"><b>${S.busy?"正在执行":"工位状态"}</b>${esc(S.step)} ${p?` · ${esc(p.sample)} ${p.command} μL · ${p.index}/${p.total}`:""}</div>`;
    if(S.error)feed+=`<div class="fail">${esc(S.error)}</div>`;
    if(S.pending)feed+=recovery();
    $("feedback").innerHTML=feed;
    const views={station:stationPage,setup:setupPage,cal:calPage,blind:blindPage,verify:verifyPage,records:recordsPage};
    const focused=document.activeElement?.id, activeVal=document.activeElement?.value;
    $("page").innerHTML=views[page]();
    if(focused&&$(focused)&&["INPUT","SELECT"].includes($(focused).tagName)){
        $(focused).focus({preventScroll:true});
        if(activeVal!=null&&document.activeElement.tagName==="INPUT")document.activeElement.value=activeVal;
    }
    if(S.busy||sending)document.querySelectorAll("button[data-action]").forEach(b=>b.disabled=true);
    if(chartObserver){chartObserver.disconnect();chartObserver=null;}
    if($("curve")){drawChart();chartObserver=new ResizeObserver(drawChart);chartObserver.observe($("curve"));}
}
function recovery(){
    const r=S.pending;const can=["dispensed","waiting_mass","measured"].includes(r.status);
    const text=r.status==="uncertain"?"现场核对枪头、瓶口和最后动作。保留本笔原始记录后，可作废并重新测量。":
      r.status==="measured"?"重量已保存，可以完成退头。":"已确认排液。核对接收瓶原状且枪头已退出后，仅补读稳定后重。";
    return `<div class="warn"><b>记录 #${r.id} · ${esc(statusName[r.status])}</b>　${text}
    <div class="actions">${can?button("recover",r.status==="measured"?"完成本笔退头":"仅补读后重","small"): ""}${button("void","核对后作废本笔","small")}</div></div>`;
}
function devicebar(){
    const arr=[["三轴",S.ports.xyz,S.position?"位置已读取":"待查询"],["移液器",S.ports.pipette,S.tip==null?"待查询":(S.tip?"已装头":"空枪")],["天平",S.ports.scale,S.weight==null?"待称重":f(S.weight,3)+" g"]];
    return '<div class="devicebar">'+arr.map(([label,port,detail])=>`<div class="device"><label>${label}</label><b>${esc(port)}</b><em>${esc(detail)}</em></div>`).join("")+"</div>";
}
function stationPage(){
    const p=S.points, liquid=S.liquid_options||{};
    const option=(key,label)=>button("liquid_option",label+" · "+(liquid[key]?"开":"关"),liquid[key]?"primary":"",!S.liquid_options,`role="switch" aria-checked="${!!liquid[key]}" data-key="${key}"`);
    const portFields=Object.entries({xyz:"三轴串口",pipette:"移液器串口",scale:"天平串口"}).map(([k,t])=>`<label>${t}<input data-draft id="port_${k}" value="${esc(form["port_"+k]??S.ports[k])}"></label>`).join("");
    const pointRows=Object.entries(keys).map(([k,name])=>{
        const coord=p[k];
        const hover=k!=="safe"&&coord;
        const origin=S.sources?.[k]||"待标定";
        const xyz=coord?(k==="safe"?"仅 Z　"+f(coord[2],2):coord.map(v=>f(v,2)).join(" / ")):"—";
        return tr(`<strong>${name}</strong>`,`<span class="tag ${coord?"":"idle"}">${esc(origin)}</span>`,xyz,
          `<div class="actions-cell">${hover?button("hover","到上方","small",!S.homed,'data-key="'+k+'"'):""} ${button("capture",coord?"重新记录":"记录","small",!S.homed,'data-key="'+k+'"')}</div>`);
    }).join("");
    const pos=(S.position||[null,null,null]).map((v,i)=>`<b>${"XYZ"[i]}　${f(v,2)}</b>`).join("");
    const jogBtns=[["z",-1,"Z− 抬高"],["z",1,"Z+ 下降"],["x",-1,"X−"],["x",1,"X+"],["y",-1,"Y−"],["y",1,"Y+"]]
      .map(([a,n,t])=>button("jog",t,"",!S.homed,`data-axis="${a}" data-dir="${n}"`)).join("");
    return head("工位设置","回零后载入保存的工位，可在点位表中微调。")+devicebar()+
      panel("连接设备",`<div class="fields">${portFields}</div><div class="actions">${button("connect",S.connected?"重新连接":"连接并检查","primary")}${button("initialize","三轴回零并初始化","",!S.connected)}${button("read","读取状态","",!S.connected)}${button("disconnect","断开","",!S.connected)}</div>
      ${S.initialized?`<div class="hint">旧程序参数已加载。设备回读　最高速度码 ${esc(S.readback.Q06??"—")}　校准系数 ${esc(S.readback.Q29??"—")}</div>`:""}`)+
      '<div class="cols equal">'+panel("点动与定位",`<div class="position">${pos}</div><small>${S.homed?"本次机械原点坐标":"未回零 · 原始编码器位置"} · mm · Z 增大时下降</small><div class="fields"><label>每次点动距离<select id="jogstep" data-pref>${(S.jog_distances_mm??[0.1,0.5,1,5,10]).map(x=>opted(String(x),x+" mm",prefs.jogstep)).join("")}</select></label></div><div class="jog">${jogBtns}</div>
      <div class="actions">${button("lift","抬到横移面","",S.clearance==null)}${button("teach_eject","在当前位置退头","",!S.connected)}</div>`)+
      panel("移液动作",`<div class="actions">${option("prewet","润洗枪头")}${option("air","预吸 50 μL 空气")}</div><p class="muted">启用预吸时，每笔最多移液 950 μL。润洗时空气留在枪头内，向接收瓶排完液体后再排出。</p>`)+'</div>'+
      panel("工位点位与微调",`<p class="muted">${S.approved?"完整工位已启用。":""}可按需要悬停查看，或在实际位置重新记录。</p>${table(["标定点","来源","X / Y / Z（mm）","操作"],pointRows)}
      <div class="actions">${button("hover","枪头 H12 上方","small",!S.homed,'data-key="tip_last"')}${button("hover","试管 B6 上方","small",!S.homed,'data-key="tube_last"')}</div>`)+
      panel("枪头盒",`<p>${S.tips<96?"下一支新枪头为 <b>"+tips[S.tips]+"</b>，当前剩余 "+(96-S.tips)+" 支。":"本盒枪头已用完。"}</p><div class="fields"><label>登记起始孔<select id="nextTip" data-pref>${options(tips,prefs.nextTip||tips[S.tips]||"A01")}</select></label>${button("tips","登记盒内起始孔")}</div><div class="actions">${button("teach_pick","取标定枪头","",!S.initialized||S.tip===true)}${button("teach_discard","到 C03 退标定头","",!S.initialized)}</div><p class="muted">每次取头会占用一个孔。换入整盒时选 A01。</p>`);
}
function noSession(){return head(pages[page][0],"先由老师完成本场课堂配置。")+panel("尚未建立课堂",button("setupPage","进入课堂配置","primary"));}
function setupPage(){
    const defaults=["A1","A2","A3","B1","B2","B3"];
    const known=L.map((n,i)=>`<div class="fieldrow"><strong>${n}</strong><label>试管位<select data-draft id="well${i}">${options(tubes,form["well"+i]??defaults[i])}</select></label><label>密度（g/mL）<input data-draft id="rho${i}" type="number" step="any" min="0" placeholder="教师填写" value="${esc(form["rho"+i]??"")}"></label></div>`).join("");
    const blind=U.map((n,i)=>`<div class="fieldrow"><strong>${n}</strong><label>试管位<select data-draft id="well${i+3}">${options(tubes,form["well"+(i+3)]??defaults[i+3])}</select></label><label>教师答案<select data-draft id="ans${i}">${options(L,form["ans"+i]??"",true)}</select></label></div>`).join("");
    return head("课堂配置","老师建立课堂并封存盲样答案。每场课堂单独保存设置和原始数据。")+
     panel("本场设置",`<div class="fields"><label>课堂名称<input data-draft id="sessionName" maxlength="60" value="${esc(form.sessionName??("移液实操 "+new Date().toLocaleDateString("zh-CN")))}"></label><label>每个标定点重复次数<input data-draft id="calRep" type="number" min="1" max="999" value="${esc(form.calRep??1)}"></label></div>`)+
     '<div class="cols equal">'+panel("三种已知液",'<div class="fieldrows">'+known+"</div>")+panel("三个盲样",'<div class="fieldrows">'+blind+"</div>")+'</div>'+
     panel("保存课堂",`<p>核对六个管位、密度和三项答案。保存后，学员提交配对时揭晓。</p>${button("new_session","建立课堂并封存答案","primary")}`)+
     (S.sessions.length?panel("已有课堂",`<div class="fields"><label>课堂记录<select id="history">${S.sessions.map(s=>opted(String(s.id),s.name+" · #"+s.id,String(S.session?.id))).join("")}</select></label>${button("open_session","打开所选课堂")}</div>`):"");
}
function rack(selection,phase){
    const m=S.session.wells;
    return '<div class="rack">'+tubes.map(w=>{
        const name=Object.keys(m).find(k=>m[k]===w);
        const enabled=(phase==="cal"?L:U).includes(name);
        return `<button data-action="source" data-sample="${esc(name)}" class="${name===selection?"selected":name?"occupied":""}" ${enabled?"":"disabled"}><strong>${w}</strong><small>${esc(name||"空位")}</small></button>`;
    }).join("")+"</div>";
}
const good=phase=>S.records.filter(r=>r.phase===phase&&r.status==="done");
const exportsHTML=()=>S.session?`<div class="actions"><a class="button primary" href="/export.csv?session=${S.session.id}">导出逐笔 CSV</a><a class="button" href="/export.json?session=${S.session.id}">完整实验记录 JSON</a></div>`:"";
const metric=(label,value,feature=false)=>`<div class="metric ${feature?"feature":""}"><small>${label}</small><b>${value}</b></div>`;
function calPage(){
    if(!S.session)return noSession();const sess=S.session, results=good("cal"), stats=S.analysis;
    const recs=L.map(name=>tr(esc(name),...V.map(v=>esc(results.filter(r=>r.sample===name&&r.command===v).length+"/"+sess.repeats)))).join("");
    const fits=L.map((n,i)=>{const m=stats.curves[n];return tr(n,m?f(m.a,5):"—",m?f(m.b,3):"—",m?f(m.r2,5):"—");}).join("");
    return head("六点标定","设定体积从 30 到 800 μL。每笔独立取头和称重，采集六点后生成拟合线。")+
    '<div class="metrics">'+metric("完成的标定记录",results.length+" / "+18*sess.repeats,true)+metric("已拟合的液体",Object.keys(stats.curves).length+" / 3")+metric("最近稳定重量",f(S.weight,3)+" g")+'</div>'+
    '<div class="cols">'+panel("体积标定曲线",`<div class="legend">${L.map((n,i)=>`<span><i style="background:${colours[i]}"></i>${n}</span>`).join("")}</div><canvas id="curve" aria-label="横轴实测转移体积、纵轴移液器设定量，单位微升"></canvas><div id="chart-note" class="chart-note">横轴为实测转移体积，纵轴为移液器设定量。</div>`)+
    panel("选择源管",rack(prefs.calSample,"cal")+`<div class="fields"><label>本次标定<select id="calSample" data-pref>${opted("all","全部三种液体",prefs.calSample)+L.map(n=>opted(n,sess.wells[n]+" · "+n,prefs.calSample)).join("")}</select></label></div><div class="actions">${button("run_cal","运行未完成的标定点","primary",!S.approved||!!S.pending)}</div>`)+'</div>'+
    panel("六点完成情况",table(["液体",...V.map(v=>v+" μL")],recs))+
    panel("拟合结果",table(["液体","斜率 a","截距 b（μL）","R²"],fits)+Object.values(stats.issues).map(x=>'<p class="warn">'+esc(x)+'</p>').join(""))+recent();
}
function blindPage(){
    if(!S.session)return noSession(); const sess=S.session, a=S.analysis, revealed=!!sess.guess;
    const scores=U.map(k=>tr(k,...L.map(n=>f(a.scores?.[k]?.[n],2)))).join("");
    const guessForm=U.map(k=>`<label>${k} · ${sess.wells[k]}<select data-guess="${k}" id="guess_${k}" ${revealed?"disabled":""}>${options(L,sess.guess?.[k]??guesses[k]??"",true)}</select></label>`).join("");
    const reveal= revealed?table(["盲样","学员配对","教师答案","结果"],U.map(k=>tr(k,esc(sess.guess[k]),esc(sess.answer[k]),sess.guess[k]===sess.answer[k]?"正确":"请复核偏差")).join("")):"";
    return head("盲样判别","按所选设定量测量盲样。比较各候选的质量偏差，决定是否追加测量，再提交三个样品的配对。")+
     '<div class="cols">'+panel("安排一组盲测",rack(prefs.blindSample,"blind")+`<div class="fields"><label>盲样<select data-pref id="blindSample">${options(U,prefs.blindSample)}</select></label><label>设定量（μL）<input data-pref id="blindVol" type="number" min="10" max="${S.liquid_options?.air?950:1000}" value="${esc(prefs.blindVol)}"></label><label>次数<input data-pref id="blindN" type="number" min="1" max="999" value="${esc(prefs.blindN)}"></label></div><p class="muted">本笔可设 10–${S.liquid_options?.air?950:1000} μL。标定范围为 30–800 μL。</p>${button("run_blind","执行这组盲测","primary",revealed||!S.approved||Object.keys(a.curves).length<3||!!S.pending)}`)+
     panel("候选质量偏差（mg）",table(["盲样",...L],scores)+`<p class="muted">同一盲样在各候选下的均方根质量偏差，越小越符合。可结合多个体积点判断。</p>${a.close?'<div class="warn">两组最佳配对的差距接近称重或标定波动，建议追加不同体积的测量。</div>':""}`)+'</div>'+
     panel("提交一一配对",`<div class="fields">${guessForm}</div><div class="actions">${button("guess","提交并揭晓","primary",revealed||Object.keys(a.scores).length<3||!!S.pending)}</div>${reveal}`)+recent();
}
function verifyPage(){
    if(!S.session)return noSession();const curves=S.analysis.curves, rec=good("verify");
    return head("200 μL 复测","回到三支已知液。把 200 μL 代入各自的标定线，用换算后的整数设定量重新移液。")+
      panel("复测计划与误差",table(["已知液","源管","计算设定 μL","下发 μL","实测 μL","绝对误差 μL","相对误差"],L.map(n=>{
          const m=curves[n],q=m?200*m.a+m.b:null,r=rec.find(x=>x.sample===n),v=r?r.net*1000/r.rho:null;
          return tr(n,S.session.wells[n],f(q),r?r.command:q==null?"—":String(Math.round(q)),f(v),v==null?"—":f(Math.abs(v-200)),v==null?"—":f((v-200)/2)+"%");
      }).join(""))+`<p class="muted">设定量取最接近的整微升。相对误差保留正负方向。</p>${button("run_verify","复测三种已知液","primary",!S.approved||!S.session.guess||rec.length===3||!!S.pending)}`)+
      panel("本场数据",exportsHTML())+recent();
}
function recordsTable(rows){
    if(!rows.length)return '<p class="empty">当前还没有测量记录。</p>';
    return table(["编号","操作","阶段","样品 / 管位","设定 μL","前重 g","后重 g","净增 g","密度 g/mL","实测 μL","枪头","状态"],
        rows.map(r=>{
            const id=S.session.answer?.[r.sample]??r.sample, rho=S.session.densities[id];
            const redo=r.phase==="cal"?button("redo","重做","small",!S.approved||!!S.pending||!["done","superseded","void"].includes(r.status),`data-id="${r.id}" aria-label="重做标定记录 ${r.id}"`):"—";
            return tr("#"+r.id+(r.redo_of?`<br><small>重做 #${r.redo_of}</small>`:""),redo,esc(phaseName[r.phase]),esc(r.sample+" / "+r.well),r.command,f(r.pre,3),f(r.post,3),f(r.net,3),f(rho,4),
              r.net!=null&&rho?f(r.net*1000/rho):"—",r.tip,`<span class="tag ${r.status==="done"?"":"idle"}">${esc(statusName[r.status]||r.status)}</span>`);
        }).join(""));
}
function recent(){return panel("最近的逐笔记录",recordsTable(S.records.slice(-4).reverse()));}
function recordsPage(){
    if(!S.session)return noSession();
    return head("数据记录",esc(S.session.name)+" · 所有尝试按编号保存，含作废记录与中断状态。")+
       panel("导出本场实验",exportsHTML())+panel("逐笔测量",'<div class="loglist">'+recordsTable([...S.records].reverse())+"</div>");
}
function drawChart(){
    const cv=$("curve");if(!cv)return;
    const ctx=cv.getContext("2d"),w=cv.clientWidth,h=cv.clientHeight;
    if(!w||!h)return;
    const dpr=window.devicePixelRatio||1;cv.width=Math.round(w*dpr);cv.height=Math.round(h*dpr);ctx.scale(dpr,dpr);
    const rows=good("cal"),points=rows.map(r=>({name:r.sample,x:r.net*1000/r.rho,y:r.command}));
    const max=Math.ceil(Math.max(800,...points.map(p=>p.x))/200)*200;
    const X=v=>48+(w-68)*v/max,Y=v=>h-38-(h-62)*v/max;
    ctx.font="11px Microsoft YaHei, sans-serif";ctx.fillStyle="#536b60";ctx.fillText("设定体积 (μL)",8,14);
    for(let i=0;i<=4;i++){let v=i*max/4;ctx.strokeStyle="#dce5df";ctx.beginPath();ctx.moveTo(X(v),Y(0));ctx.lineTo(X(v),Y(max));ctx.moveTo(X(0),Y(v));ctx.lineTo(X(max),Y(v));ctx.stroke();ctx.fillText(String(v),X(v)-7,Y(0)+15);ctx.fillText(String(v),12,Y(v)+3);}
    ctx.fillText("实测转移体积 (μL)",Math.max(46,w-120),h-4);
    L.forEach((n,i)=>{ctx.strokeStyle=ctx.fillStyle=colours[i];ctx.lineWidth=2;const fit=S.analysis.curves[n],ps=points.filter(p=>p.name===n);
      if(fit&&ps.length){const lo=Math.min(...ps.map(p=>p.x)),hi=Math.max(...ps.map(p=>p.x));ctx.beginPath();ctx.moveTo(X(lo),Y(fit.a*lo+fit.b));ctx.lineTo(X(hi),Y(fit.a*hi+fit.b));ctx.stroke();}
      for(const p of ps){ctx.beginPath();ctx.arc(X(p.x),Y(p.y),3.8,0,Math.PI*2);ctx.fill();}});
    cv.onpointermove=e=>{const bb=cv.getBoundingClientRect(),x=e.clientX-bb.left,y=e.clientY-bb.top;
      const p=points.find(p=>Math.hypot(X(p.x)-x,Y(p.y)-y)<12);
      $("chart-note").textContent=p?p.name+" · 实测 "+f(p.x)+" μL · 设定 "+p.y+" μL":"横轴为实测转移体积，纵轴为移液器设定量。";};
}
document.addEventListener("input",readDraft);
document.addEventListener("change",()=>{readDraft();if(["calSample","blindSample"].includes(document.activeElement?.id))render(true);});
document.addEventListener("click",async e=>{
    const nav=e.target.closest("[data-nav]");if(nav){readDraft();setPage(nav.dataset.nav);return;}
    const b=e.target.closest("[data-action]");if(!b||b.disabled)return;
    const a=b.dataset.action,k=b.dataset.key;readDraft();
    try{
      if(a==="setupPage"){setPage("setup");return;}
      if(a==="source"){prefs[page==="cal"?"calSample":"blindSample"]=b.dataset.sample;render(true);return;}
      if(a==="connect"){await send(a,{ports:Object.fromEntries(["xyz","pipette","scale"].map(n=>[n,val("port_"+n)]))});return;}
      if(a==="new_session"){
        const all=[...L,...U], defs=Object.fromEntries(L.map((n,i)=>[n,Number(val("rho"+i))]));
        if(Object.values(defs).some(x=>!(x>0))){toast("请填写全部三种已知液密度");return;}
        const data={name:val("sessionName"),repeats:Number(val("calRep")),wells:Object.fromEntries(all.map((n,i)=>[n,val("well"+i)])),
                    densities:defs,answer:Object.fromEntries(U.map((n,i)=>[n,val("ans"+i)]))};
        if(await confirmAction("封存教师答案","保存后会进入课堂实验。请核对六个试管位和三个盲样答案。"))await send(a,data);
        return;
      }
      if(a==="open_session"){await send(a,{id:Number(val("history"))});return;}
      if(a==="capture"){if(await confirmAction("记录"+keys[k],notes[k]+" 将以当前实测坐标保存。"))await send(a,{key:k});return;}
      if(a==="jog"){await send(a,{axis:b.dataset.axis,delta:Number(b.dataset.dir)*Number(prefs.jogstep)});return;}
      if(a==="hover"){await send(a,{key:k});return;}
      if(a==="tips"){const next=val("nextTip");if(await confirmAction("更新枪头盒","请确认从 "+next+" 起的枪头均为新枪头。"))await send(a,{next});return;}
      if(a==="liquid_option"){await send(a,{key:k,enabled:!S.liquid_options[k]});return;}
      if(a==="redo"){await send("redo",{id:Number(b.dataset.id)});return;}
      if(a==="run_cal"){await send("run",{phase:"cal",sample:prefs.calSample});return;}
      if(a==="run_blind"){await send("run",{phase:"blind",sample:prefs.blindSample,volume:Number(prefs.blindVol),repeats:Number(prefs.blindN)});return;}
      if(a==="run_verify"){await send("run",{phase:"verify"});return;}
      if(a==="guess"){if(await confirmAction("提交盲样配对","提交后将揭晓老师录入的答案，本场配对即锁定。"))await send(a,{pair:{...guesses}});return;}
      const msgs={initialize:"确认空枪且回零路径畅通。三轴会依次沿 Z 向上、X 负向、Y 负向寻限位并退离，然后初始化移液器。",teach_eject:"确认当前枪头在废头容器开口内，执行弹头。",
        recover:"确认接收瓶保持原状，枪头已退出。仅补读重量或完成已记录的退头。",
        void:"请核对接收瓶和枪头。本次尝试将作废，保留全部原始记录。"};
      if(msgs[a]&&!await confirmAction(b.textContent,msgs[a]))return;
      await send(a,{confirmed:true,id:S.pending?.id});
    }catch(ex){toast(ex.message);}
});
$("stop").onclick=async()=>{try{await post("/api/stop",{});await poll(true);}catch(e){toast(e.message);}};
window.addEventListener("hashchange",()=>{const k=location.hash.slice(1);if(pages[k]){page=k;render(true);}});
poll();timer=setInterval(()=>poll(),900);
