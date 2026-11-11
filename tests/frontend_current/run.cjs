const {spawnSync}=require('node:child_process');
const path=require('node:path');
for(const name of ['test_draft_vault.cjs','test_draft_editor.cjs','test_import_staging.cjs','test_import_legacy.cjs']) {
 const r=spawnSync(process.execPath,[path.join(__dirname,name)],{stdio:'inherit'});
 if(r.status!==0) process.exit(r.status||1);
}
