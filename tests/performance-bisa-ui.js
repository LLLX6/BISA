'use strict';
const fs=require('fs');const path=require('path');
const files=['index.html','assets/styles/bisa.css','assets/scripts/bisa-app.js'].map(file=>({file,bytes:fs.statSync(path.resolve(__dirname,'..',file)).size}));
const total=files.reduce((sum,item)=>sum+item.bytes,0);
if(total>600000)throw new Error(`public_shell_too_large:${total}`);
console.log(JSON.stringify({ok:true,publicShellBytes:total,files}));
