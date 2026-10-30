'use strict';
let sales=[],monthlySales=[];
const finCents=id=>{let s=$(id).value.trim().replace(/\s/g,'').replace(',','.');if(!/^\d+(\.\d{1,2})?$/.test(s))throw Error('Введите корректную сумму.');let v=Math.round(Number(s)*100);if(!Number.isSafeInteger(v))throw Error('Слишком большая сумма.');return v};
async function refreshFinance(){
 if(!me)return;
 try{const [d,m]=await Promise.all([api('sales'),api('monthly-sales')]);sales=d.sales;monthlySales=m.monthly;renderFinance()}
 catch(e){report('Продажи: '+e.message,true)}
}
function renderFinance(){
 if(!me)return;
 const month=$('month').value,monthly=sales.filter(s=>s.day.startsWith(month));
 const sum=(list,key)=>list.reduce((a,x)=>a+Number(x[key]||0),0);
 const override=monthlySales.find(x=>x.month===month);
 const revenue=override?Number(override.revenue):sum(monthly,'revenue'),gross=override?Number(override.gross):sum(monthly,'gross'),checks=override?Number(override.checks):sum(monthly,'checks');
 const costs=rows.filter(x=>x.scope==='Магазин'&&x.date.startsWith(month)&&!/(закуп|товар|поставщик|поступление)/i.test(x.category)).reduce((a,x)=>a+x.amount,0);
 $('monthlyMode').textContent=override?'Итоги за месяц введены вручную. Дневные записи не прибавляются повторно.':'Итоги рассчитаны из продаж по дням. Можно вместо этого ввести общие продажи за месяц.';
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

$('newMonthSale').onclick=()=>{
 const m=$('month').value,x=monthlySales.find(v=>v.month===m);
 $('monthSaleMonth').value=m;$('monthSaleRevenue').value=x?String(x.revenue/100):'';
 $('monthSaleGross').value=x?String(x.gross/100):'';$('monthSaleChecks').value=x?.checks??'';
 $('monthSaleNote').value=x?.note||'';$('deleteMonthSale').hidden=!x;
 $('monthSaleDialog').showModal();
};
$('closeMonthSale').onclick=()=>$('monthSaleDialog').close();
$('monthSaleForm').onsubmit=async e=>{
 e.preventDefault();
 try{
  const x={month:$('monthSaleMonth').value,revenue:finCents('monthSaleRevenue'),gross:finCents('monthSaleGross'),checks:Number($('monthSaleChecks').value),note:$('monthSaleNote').value};
  if(x.gross>x.revenue)throw Error('Валовая прибыль не может превышать выручку.');
  await send('monthly-sales',x);$('monthSaleDialog').close();$('month').value=x.month;
  await refreshFinance();report('Месячные итоги сохранены.')
 }catch(e){report(e.message,true)}
};
$('deleteMonthSale').onclick=async()=>{
 const m=$('monthSaleMonth').value;if(!confirm('Удалить месячные итоги за '+m+'?'))return;
 try{await api('monthly-sales/'+m,{method:'DELETE'});$('monthSaleDialog').close();await refreshFinance();report('Месячная запись удалена.')}
 catch(e){report(e.message,true)}
};
