'use strict';

const {chromium} = require('playwright');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const source = path.join(root, 'assets', 'brand', 'bisa-mark.svg');
const outputs = [
  {name: 'apple-touch-icon.png', size: 180, maskable: false},
  {name: 'app-icon-192.png', size: 192, maskable: false},
  {name: 'app-icon-512.png', size: 512, maskable: false},
  {name: 'app-icon-maskable-192.png', size: 192, maskable: true},
  {name: 'app-icon-maskable-512.png', size: 512, maskable: true},
];

async function main() {
  const svg = fs.readFileSync(source).toString('base64');
  const browser = await chromium.launch({headless: true});
  try {
    for (const output of outputs) {
      const page = await browser.newPage({
        viewport: {width: output.size, height: output.size},
        deviceScaleFactor: 1,
      });
      const imageRule = output.maskable
        ? 'img{display:block;width:78%;height:78%;margin:11%;}'
        : 'img{display:block;width:100%;height:100%;}';
      await page.setContent(
        `<style>html,body{width:100%;height:100%;margin:0;overflow:hidden;background:#FFF7F1}${imageRule}</style>`
        + `<img alt="" src="data:image/svg+xml;base64,${svg}">`,
        {waitUntil: 'load'},
      );
      const rootTarget = path.join(root, output.name);
      const publicTarget = path.join(root, 'public', output.name);
      await page.screenshot({path: rootTarget, type: 'png', omitBackground: false});
      fs.copyFileSync(rootTarget, publicTarget);
      await page.close();
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(`${JSON.stringify({ok: true, source: 'assets/brand/bisa-mark.svg', outputs})}\n`);
}

main().catch(error => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
