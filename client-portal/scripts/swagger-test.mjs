import assert from 'node:assert/strict';

// Check the rendered gateway UI: a valid OpenAPI document alone misses absent CSS.
export async function checkSwagger(page, gateway, manifest) {
 const errors=[];
 const onError=error=>errors.push(error.message);
 page.on('pageerror',onError);
 try {
  const response=await page.goto(gateway+'/api/docs');
  assert.equal(response.status(),200);
  assert.equal(await page.title(),'API documentation');
  const stylesheet=page.locator('link[rel="stylesheet"][href$="/swagger-ui.css"]');
  assert.equal(await stylesheet.count(),1,'Swagger stylesheet link is missing');
  await page.waitForFunction(()=>{
   const link=document.querySelector('link[rel="stylesheet"][href$="/swagger-ui.css"]');
   return !!link?.sheet;
  });
  const operations=page.locator('.swagger-ui .opblock');
  await operations.first().waitFor();
  assert.ok(await operations.count()>0,'Swagger did not render API operations');
  for(const service of manifest.services)for(const route of service.routes){
   assert.ok(await page.locator('.opblock-summary-path').filter({hasText:route.list}).count()>0,
    'Swagger did not render '+route.list);
  }
  // Both properties depend on Swagger's stylesheet, not browser defaults.
  const summary=page.locator('.swagger-ui .opblock-summary').first();
  assert.equal(await summary.evaluate(el=>getComputedStyle(el).display),'flex');
  assert.ok(await operations.first().evaluate(el=>parseFloat(getComputedStyle(el).borderTopWidth)>0));
  const operation=page.locator('.swagger-ui .opblock.opblock-get').first();
  await operation.locator('.opblock-summary-control').click();
  await operation.locator('.opblock-body').waitFor();
  assert.deepEqual(errors,[],'Swagger runtime errors');
 } finally {
  page.off('pageerror',onError);
 }
}
