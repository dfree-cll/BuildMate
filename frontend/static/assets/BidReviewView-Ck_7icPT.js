import{b as V,a as C,d as B}from"./index-Cwii2006.js";import{d as N,o as w,i as g,w as i,r as p,a as _,b as y,f as b,t as h,k as F,g as m}from"./index-CWS2fkbw.js";import"./client-_t25d5zr.js";const P={style:{"white-space":"pre-wrap"}},j=N({__name:"BidReviewView",setup(T){const u=m(""),c=m(null),f=m(!1),o=m("");function x(n){c.value=n.raw||n,u.value="[文件: "+n.name+" 已选择，点击开始评审]"}async function k(){var n,t,d;f.value=!0,o.value="⏳ 已提交，四维并行评审中...";try{let a;c.value?a=(await V(c.value)).data.review_id:a=(await C(u.value)).data.review_id;for(let s=0;s<45;s++){await new Promise(l=>setTimeout(l,2e3));const e=(await B(a)).data;if(e.status==="processing"){o.value="⏳ 评审中（"+(s+1)+"）...";continue}if(e.status==="failed"){o.value="❌ 评审失败："+(e.error||"");break}let r="🏆 综合得分："+e.weighted_score+` / 100

📊 各维度：
`;for(const l of e.dimensions||[])r+="  • "+l.dimension+"："+l.score+` 分
`;if(e.summary&&(r+=`
📝 `+(e.summary.overall_comment||"")+`
✅ 结论：`+(e.summary.recommendation||"")),(n=e.issues)!=null&&n.length){r+=`

⚠️ 风险问题：
`;for(const l of e.issues.slice(0,5))r+="  • ["+l.priority+"] "+l.description+`
`}o.value=r;break}}catch(a){o.value="❌ 评审失败："+(((d=(t=a.response)==null?void 0:t.data)==null?void 0:d.detail)||a.message)}f.value=!1}return(n,t)=>{const d=p("el-upload"),a=p("el-input"),s=p("el-button"),v=p("el-card");return w(),g(v,null,{header:i(()=>[...t[1]||(t[1]=[b("📄 投标文件四维并行评审",-1)])]),default:i(()=>[_(d,{drag:"","auto-upload":!1,"on-change":x,limit:1,accept:".pdf",style:{"margin-bottom":"12px"}},{default:i(()=>[...t[2]||(t[2]=[y("div",{style:{padding:"20px"}},"📎 拖拽或点击上传 PDF 投标文件（或粘贴文本）",-1)])]),_:1}),_(a,{modelValue:u.value,"onUpdate:modelValue":t[0]||(t[0]=e=>u.value=e),type:"textarea",rows:4,placeholder:"或直接粘贴投标文件文本（留空则用模拟文档）",style:{"margin-bottom":"12px"}},null,8,["modelValue"]),_(s,{type:"primary",loading:f.value,onClick:k},{default:i(()=>[...t[3]||(t[3]=[b("开始评审",-1)])]),_:1},8,["loading"]),o.value?(w(),g(v,{key:0,style:{"margin-top":"16px",background:"#f9fbe7"}},{default:i(()=>[y("pre",P,h(o.value),1)]),_:1})):F("",!0)]),_:1})}}});export{j as default};
