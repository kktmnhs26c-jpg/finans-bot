'use strict';
let sales=[];
const finCents=id=>{let s=$(id).value.trim().replace(/\s/g,'').replace(',','.');if(!/^\d+(\.\d{1,2})?$/.test(s))throw Error('Введите корректную сумму.');let v=Math.round(Number(s)*100);if(!Number.isSafeInteger(v))throw Error('Слишком большая сумма.');return v};
async function refreshFinance(){
 if(!me)return;
 try{sales=(await api('sales')).sales;renderFinance()}
 catch(e){report('Продажи: '+e.message,true)}
}
function renderFinance(){
 if(!me)return;
 const month=$('month').value,monthly=sales.filter(s=>s.day.startsWith(month));
 const sum=(list,key)=>list.reduce((a,x)=>a+Number(x[key]||0),0);
 const revenue=sum(monthly,'revenue'),gross=sum(monthly,'gross'),checks=sum(monthly,'checks');
 const costs=rows.filter(x=>x.scope==='Магазин'&&x.date.startsWith(month)&&!/(закуп|товар|поставщик|поступление)/i.test(x.category)).reduce((a,x)=>a+x.amount,0);
 $('finRevenue').textContent=money(revenue);$('finGross').textContent=money(gross);$('finCosts').textContent=money(costs);
 $('finNet').textContent=money(gross-costs);$('finChecks').textContent=checks.toLocaleString('ru-RU');
 $('finAvg').textContent='Средний чек: '+money(checks?Math.round(revenue/checks):0);
 $('finMargin').textContent=revenue?((gross-costs)/revenue*100).toFixed(1)+'%':'—';
 $('salesRows').replaceChildren();
 for(const s of monthly){
  let tr=document.createElement('tr');tr.append(td(s.day.split('-').reverse().join('.')),td(money(s.revenue)),td(money(s.gross)),td(String(s.checks)),td(money(s.checks?Math.round(s.revenue/s.checks):0)));
  if(me.role==='owner'){
   let actions=td(''),edit=document.createElement('button'),del=document.createElement('button');
   edit.textContent='Изменить';edit.onclick=()=>openSale(s);
   del.textContent='Удалить';del.onclick=async()=>{if(!confirm('Удалить продажи за '+s.day+'?'))return;try{await api('sales/'+s.day,{method:'DELETE'});await refreshFinance()}catch(e){report(e.message,true)}};
   actions.append(edit,del);tr.append(actions)
  }$('salesRows').append(tr)
 }
}
function openSale(s){
 $('saleDay').value=s?.day||today();$('saleRevenue').value=s?String(s.revenue/100):'';
 $('saleGross').value=s?String(s.gross/100):'';$('saleChecks').value=s?.checks??'';
 $('saleNote').value=s?.note||'';$('saleDialog').showModal()
}
$('newSale').onclick=()=>openSale(null);
$('closeSale').onclick=()=>$('saleDialog').close();
$('saleForm').onsubmit=async e=>{
 e.preventDefault();
 try{
  const item={day:$('saleDay').value,revenue:finCents('saleRevenue'),gross:finCents('saleGross'),checks:Number($('saleChecks').value),note:$('saleNote').value};
  if(item.gross>item.revenue)throw Error('Валовая прибыль не может превышать выручку.');
  await send('sales',item);$('saleDialog').close();$('month').value=item.day.slice(0,7);
  await refreshFinance();renderFinance();report('Продажи сохранены в общей базе.')
 }catch(e){report(e.message,true)}
};
const oldRefresh=refresh;
refresh=async function(){await oldRefresh();if(me)await refreshFinance()};
const oldRender=render;
render=function(){oldRender();renderFinance()};
