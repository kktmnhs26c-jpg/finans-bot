'use strict';
const $=id=>document.getElementById(id);
const money=v=>(v/100).toLocaleString('ru-RU',{style:'currency',currency:'RUB'});
const today=()=>{let d=new Date();return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')};
let me=null,rows=[],editing=null;
function display(name){for(let n of ['auth','setup','offline','dashboard'])$(n).classList.toggle('hidden',n!==name);$('logout').hidden=name!=='dashboard'}
function report(s,error=false){$('notice').hidden=false;$('notice').textContent=s;$('notice').className=error?'error':''}
async function api(url,opt={}){
  let r;try{r=await fetch('/api/'+url,{credentials:'same-origin',headers:{'Content-Type':'application/json'},...opt})}
  catch(e){throw Error('Нет соединения с сайтом.')}
  const j=await r.json().catch(()=>({error:'Ошибка сервера'}));
  if(!r.ok){let e=Error(j.error||'Ошибка '+r.status);e.status=r.status;throw e}return j;
}
function send(url,data,method='POST'){return api(url,{method,body:JSON.stringify(data)})}
async function boot(){
 try{
  const s=await api('me');
  if(!s.authenticated){display(s.needs_setup?'setup':'auth');const token=new URL(location).searchParams.get('setup');if(token)$('setupCode').value=token;return}
  me=s;display('dashboard');$('who').textContent=s.role==='owner'?'Владелец · управление':'Партнёр · просмотр';
  $('roleHelp').textContent=s.role==='owner'?'Все изменения сохраняются в общей базе.':'Вы можете смотреть расходы, но не изменять их.';
  document.querySelectorAll('.owner').forEach(x=>x.hidden=s.role!=='owner');
  if(s.role==='partner'&&s.scope==='shop'){$('scope').value='Магазин';$('scope').disabled=true}
  if(s.role==='owner')await partnerStatus();
  await refresh();
 }catch(e){display(e.status===503?'offline':'auth');report(e.message,true)}
}
$('loginForm').onsubmit=async e=>{e.preventDefault();try{await send('login',{username:$('username').value,password:$('loginPassword').value});$('loginPassword').value='';await boot()}catch(e){report(e.message,true)}};
$('setupForm').onsubmit=async e=>{e.preventDefault();try{await send('setup',{setupCode:$('setupCode').value,password:$('setupPassword').value});history.replaceState(null,'',location.pathname);$('setupCode').value='';$('setupPassword').value='';await boot();report('Аккаунт владельца создан. Добавьте расходы и создайте доступ партнёра.')}catch(e){report(e.message,true)}};
$('passwordShortcut').onclick=()=>{
  $('passwordPanel').scrollIntoView({behavior:'smooth',block:'start'});
  $('currentOwnerPassword').focus({preventScroll:true});
};
$('logout').onclick=async()=>{try{await send('logout',{});display('auth');me=null;rows=[];report('Вы вышли.')}catch(e){report(e.message,true)}};
function filtered(){
 const month=$('month').value,term=$('search').value.toLowerCase(),cat=$('category').value,scope=$('scope').value;
 return rows.filter(r=>r.date.startsWith(month)&&(scope==='all'||r.scope===scope)&&(cat==='all'||r.category===cat)&&[r.category,r.note,r.payment].join(' ').toLowerCase().includes(term)).sort((a,b)=>b.date.localeCompare(a.date));
}
function td(s){const v=document.createElement('td');v.textContent=s;return v}
function render(){
 const cats=[...new Set(rows.map(r=>r.category))].sort((a,b)=>a.localeCompare(b,'ru')),chosen=$('category').value;
 $('category').replaceChildren(new Option('Все категории','all'),...cats.map(c=>new Option(c,c)));$('category').value=cats.includes(chosen)?chosen:'all';
 $('cats').replaceChildren(...cats.map(c=>new Option(c,c)));
 const list=filtered(),sum=list.reduce((a,r)=>a+r.amount,0);
 $('total').textContent=money(sum);$('count').textContent=list.length+' операций';
 $('shopTotal').textContent=money(list.filter(r=>r.scope==='Магазин').reduce((a,r)=>a+r.amount,0));
 $('personalTotal').textContent=money(list.filter(r=>r.scope==='Личные').reduce((a,r)=>a+r.amount,0));
 $('rows').replaceChildren();$('empty').hidden=list.length>0;
 for(const r of list){
   const tr=document.createElement('tr'),first=td(r.category);
   if(r.note){const small=document.createElement('small');small.textContent=r.note;first.append(small)}
   tr.append(first,td(r.date.endsWith('-00')?'День неизвестен · '+r.date.slice(5,7)+'.'+r.date.slice(0,4):r.date.split('-').reverse().join('.')),td(r.scope),td(r.payment),td(money(r.amount)));
   if(me.role==='owner'){
     const actions=td(''),edit=document.createElement('button'),del=document.createElement('button');
     edit.textContent='Изменить';edit.onclick=()=>openEditor(r);
     del.textContent='Удалить';del.onclick=async()=>{if(!confirm('Удалить «'+r.category+'» на '+money(r.amount)+'?'))return;try{await api('expenses/'+encodeURIComponent(r.id),{method:'DELETE'});await refresh();report('Расход удалён.')}catch(e){report(e.message,true)}};
     actions.append(edit,del);tr.append(actions);
   }
   $('rows').append(tr);
 }
}
async function refresh(){
 try{const r=await api('expenses');rows=r.expenses;$('refreshTime').textContent=new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});$('sync').textContent='Общая база · '+rows.length+' записей';render()}
 catch(e){report(e.message,true);if(e.status===401)boot()}
}
$('refresh').onclick=refresh;
for(const id of ['scope','month','category'])$(id).onchange=render;
$('search').oninput=render;
function openEditor(r=null){
 editing=r?.id||null;$('formHeading').textContent=r?'Редактировать расход':'Новый расход';
 $('amount').value=r?String(r.amount/100):'';$('date').value=r&&!r.date.endsWith('-00')?r.date:today();
 $('editScope').value=r?.scope||'Магазин';$('editCategory').value=r?.category||'';$('payment').value=r?.payment||'Карта';$('note').value=r?.note||'';
 $('dialog').showModal();
}
$('add').onclick=()=>openEditor();$('close').onclick=()=>$('dialog').close();
$('expenseForm').onsubmit=async e=>{
 e.preventDefault();const raw=$('amount').value.replace(/\s/g,'').replace(',','.');
 if(!/^\d+(\.\d{1,2})?$/.test(raw)){report('Неверная сумма.',true);return}
 const item={id:editing||crypto.randomUUID(),amount:Math.round(Number(raw)*100),date:$('date').value,scope:$('editScope').value,category:$('editCategory').value.trim(),payment:$('payment').value,note:$('note').value.trim()};
 try{await send('expenses',{expense:item});$('dialog').close();$('month').value=item.date.slice(0,7);$('scope').value='all';$('search').value='';$('category').value='all';await refresh();report('Сохранено в общей базе.')}catch(e){report(e.message,true)}
};
$('ownerPasswordForm').onsubmit=async e=>{
 e.preventDefault();
 try{
  await send('password',{currentPassword:$('currentOwnerPassword').value,newPassword:$('newOwnerPassword').value},'PUT');
  $('currentOwnerPassword').value='';$('newOwnerPassword').value='';
  report('Пароль владельца изменён. Используйте новый пароль на всех устройствах.');
 }catch(e){report(e.message,true)}
};
async function partnerStatus(){try{const p=await api('partner');$('partnerScope').value=p.scope;$('partnerStatus').textContent=p.configured?'Пароль партнёра установлен. Новый пароль завершит старые сеансы партнёра.':'У партнёра пока нет доступа.'}catch(e){report(e.message,true)}}
$('partnerForm').onsubmit=async e=>{e.preventDefault();try{await send('partner',{scope:$('partnerScope').value,password:$('partnerPassword').value},'PUT');$('partnerPassword').value='';await partnerStatus();report('Доступ партнёра готов. Передайте ему адрес сайта и пароль.')}catch(e){report(e.message,true)}};
$('exportBtn').onclick=async()=>{try{const data=await api('export'),url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download='rashody-backup-'+today()+'.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),5000);report('Резервная копия подготовлена.')}catch(e){report(e.message,true)}};
$('importBtn').onclick=()=>$('importFile').click();
function parseCsv(t){
 const lines=t.replace(/^\uFEFF/,'').split(/\r?\n/).filter(Boolean),sep=lines[0]?.includes(';')?';':',',heads=lines.shift().split(sep).map(v=>v.toLowerCase().replace(/["\s₽]/g,''));
 const col=x=>heads.indexOf(x);
 if(['дата','сумма','раздел','категория'].some(x=>col(x)<0))throw Error('Для CSV обязательны дата, сумма, раздел, категория. Надёжнее загрузить резервную копию JSON.');
 return lines.map((l,i)=>{const a=l.split(sep).map(v=>v.replace(/^"|"$/g,''));return {id:'cloud-import-'+crypto.randomUUID(),amount:Math.round(Number(a[col('сумма')].replace(',','.'))*100),date:a[col('дата')],scope:a[col('раздел')],category:a[col('категория')],payment:a[col('оплата')]||'Не указано',note:a[col('комментарий')]||''}});
}
$('importFile').onchange=async e=>{
 const file=e.target.files?.[0];if(!file)return;
 try{
  if(file.size>6*1024*1024)throw Error('Максимальный размер 6 МБ.');
  const text=await file.text(),raw=file.name.toLowerCase().endsWith('.csv')?parseCsv(text):JSON.parse(text),list=Array.isArray(raw)?raw:raw.expenses;
  if(!Array.isArray(list))throw Error('Файл не содержит расходов.');
  const sum=list.reduce((a,r)=>a+(Number(r.amount)||0),0);
  if(!confirm('Загрузить '+list.length+' расходов на '+money(sum)+' в общую базу?'))return;
  const result=await send('import',{expenses:list});
  await refresh();report('Импорт: добавлено '+result.added+', повторных '+result.duplicates+'.');
 }catch(err){report(err.message,true)}finally{e.target.value=''}
};
$('month').value=today().slice(0,7);boot();