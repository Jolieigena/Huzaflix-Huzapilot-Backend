"""Run the generated internal-network checker without publishing telemetry ports."""
import json

def run_checks(manifest,check,cmd,compose,root):
    if not manifest.get('monitoring',{}).get('enabled'):return
    def verify():
        raw=cmd(compose+['run','--rm','--no-deps','-T','monitoring-check'],timeout=420)
        report=json.loads(raw.strip().splitlines()[-1])
        if report['passed']!=report['total']:raise AssertionError(json.dumps(report))
        from pathlib import Path
        (root/'monitoring-results.json').write_text(json.dumps(report,indent=2))
    check('monitoring metrics, logs, dashboards and isolation',verify)

    def recovery():
        import time
        report=json.loads((root/'monitoring-results.json').read_text())
        before=time.time()-1
        collectors=['monitoring-prometheus','monitoring-loki','monitoring-alloy']
        try:
            cmd(compose+['stop',*collectors],timeout=90)
            # Read real application data with caching bypassed while collection is down.
            route=manifest['services'][0]['routes'][0]['list']
            script="import json,os,urllib.request; base='http://api-gateway:8080'; login=urllib.request.Request(base+'/auth/login',data=json.dumps({'username':os.environ['AUTH_USERNAME'],'password':os.environ['AUTH_PASSWORD']}).encode(),headers={'Content-Type':'application/json'}); token=json.load(urllib.request.urlopen(login))['access_token']; response=urllib.request.urlopen(urllib.request.Request(base+"+repr(route)+",headers={'Authorization':'Bearer '+token,'X-Cache-Bypass':'1'})); assert response.status==200;print('Application database read succeeded with collectors stopped')"
            cmd(compose+['exec','-T','identity','python','-c',script],timeout=45)
        finally:
            cmd(compose+['start',*collectors],timeout=90)
        raw=cmd(compose+['run','--rm','--no-deps','-T','monitoring-check','python','monitoring/check.py','--existing-log-id',report['probeRequestId'],'--history-timestamp',str(before)],timeout=420)
        recovered=json.loads(raw.strip().splitlines()[-1])
        if recovered['passed']!=recovered['total']:raise AssertionError(json.dumps(recovered))
        (root/'monitoring-recovery-results.json').write_text(json.dumps(recovered,indent=2))
    check('monitoring outage isolation and persisted telemetry after restart',recovery)

    def browser():
        import subprocess
        report=root/'monitoring-results';report.mkdir(exist_ok=True)
        container=manifest['project']+'-monitoring-browser'
        try:
            cmd(['docker','run','--rm','--name',container,'--network','host','--ipc','host','-e','GRAFANA_PORT','-e','PROJECT_ROOT=/project','-v',str(root)+':/project:ro','-v',str(root/'monitoring/browser.mjs')+':/runner/monitoring-browser.mjs:ro','-v',str(report)+':/results','huza-smoke-browser','node','monitoring-browser.mjs'],timeout=240)
        finally:
            subprocess.run(['docker','rm','-f',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
    check('Grafana browser login and all provisioned dashboards',browser)
