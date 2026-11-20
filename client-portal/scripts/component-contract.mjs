/** AST-based adjacent-CSS coverage, no type escapes, and declared design-token checks. */
import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';
function walk(dir){return fs.readdirSync(dir,{withFileTypes:true}).flatMap(e=>e.isDirectory()?walk(path.join(dir,e.name)):[path.join(dir,e.name)]);}
const files=walk('src');const failures=[];let classes=0;
const css=files.filter(f=>f.endsWith('.css')).map(f=>fs.readFileSync(f,'utf8').replace(/\/\*[\s\S]*?\*\//g,''));
const declared=new Set(css.flatMap(s=>[...s.matchAll(/(--[\w-]+)\s*:/g)].map(m=>m[1])));
for(const text of css)for(const m of text.matchAll(/var\((--[\w-]+)/g))if(!declared.has(m[1])&&!m[1].startsWith('--radix-'))failures.push(`Undefined token ${m[1]}`);
for(const file of files.filter(f=>f.endsWith('.tsx'))){
 const text=fs.readFileSync(file,'utf8');const style=file.replace(/\.tsx$/,'.css');if(!fs.existsSync(style)){failures.push(`${file}: missing adjacent CSS`);continue;}
 const styles=fs.readFileSync(style,'utf8').replace(/\/\*[\s\S]*?\*\//g,'');const source=ts.createSourceFile(file,text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 function classText(node){if(ts.isStringLiteral(node)||ts.isNoSubstitutionTemplateLiteral(node))return node.text;if(ts.isTemplateExpression(node))return node.head.text+' '+node.templateSpans.map(s=>s.literal.text).join(' ');if(ts.isCallExpression(node)&&ts.isIdentifier(node.expression)&&node.expression.text==='classes'&&node.arguments[0])return classText(node.arguments[0]);return '';}
 function visit(node){
  if(ts.isJsxAttribute(node)&&node.name.getText()==='className'&&node.initializer){const value=ts.isJsxExpression(node.initializer)?node.initializer.expression:node.initializer;if(value)for(const name of classText(value).trim().split(/\s+/).filter(Boolean)){classes++;if(!new RegExp('\\.'+name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'(?![\\w-])').test(styles))failures.push(`${file}: ${name} missing from ${style}`);}}
  if(ts.isJsxAttribute(node)&&node.name.getText()==='style')failures.push(`${file}: inline styles are not allowed`);
  if(node.kind===ts.SyntaxKind.AnyKeyword)failures.push(`${file}: any type is not allowed`);
  ts.forEachChild(node,visit);
 }
 visit(source);if(/@ts-(ignore|nocheck)/.test(text))failures.push(`${file}: suppressed TypeScript checks`);
}
if(failures.length){console.error([...new Set(failures)].join('\n'));process.exitCode=1;}else console.log(`Component contract: ${classes} static class hooks covered; token references resolved; no inline styles or type escapes.`);

if (!failures.length && fs.existsSync('src/studio/assemblies/catalog.json')) await import('./assembly-contract.mjs');
