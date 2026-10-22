from pathlib import Path
from datetime import datetime
import re, shutil, subprocess, sys

ROOT = Path.cwd()
HTML = ROOT / 'public/index.html'
BACKEND = ROOT / 'backend/server.py'

if not HTML.is_file() or not BACKEND.is_file():
    sys.exit('ERRO: execute em /public/pre-o-certo, onde existem public/index.html e backend/server.py.')

html = HTML.read_text(encoding='utf-8')
backend = BACKEND.read_text(encoding='utf-8')

if 'PC_ACCOUNT_SETTINGS_V1' in html and 'PC_ACCOUNT_API_V1' in backend:
    print('As configurações já parecem instaladas. Não fiz alterações duplicadas.')
    sys.exit(0)

# Corrige apenas sequências típicas de texto UTF-8 interpretado como Windows-1252/Latin-1.
fixes = {
    'Ã¡':'á','Ã ': 'à','Ã¢':'â','Ã£':'ã','Ã¤':'ä','Ã§':'ç','Ã©':'é','Ãª':'ê','Ã­':'í',
    'Ã³':'ó','Ã´':'ô','Ãµ':'õ','Ãº':'ú','Ã¼':'ü','Ã‰':'É','Ã€':'À','Ã‚':'Â','Ãƒ':'Ã',
    'Ã‡':'Ç','Ã“':'Ó','Ã”':'Ô','Ã•':'Õ','Ãš':'Ú','Ãœ':'Ü','Âº':'º','Âª':'ª','Â·':'·',
    'â€“':'–','â€”':'—','â€œ':'“','â€':'”','â€˜':'‘','â€™':'’','ï¿½':'�'
}
for bad, good in fixes.items():
    html = html.replace(bad, good)

# Tema claro/escuro salvo no navegador; o botão flutuante antigo fica oculto.
theme_css = '''
/* PC_ACCOUNT_SETTINGS_V1 */
body.dark{--bg:#101214;--card:#1b1e21;--ink:#f3f4f6;--mute:#a7adb5;--line:#343940;--gl:#153d32;--gd:#a7f3d0;--dark:#080a0b;--warn:#463813;--warnt:#fbbf24;color-scheme:dark}
body.dark .nav,body.dark .bar{background:var(--bg);color:var(--ink)}
body.dark input,body.dark select,body.dark textarea{background:var(--card);color:var(--ink);border-color:var(--line)}
#pc-theme-toggle,.pc-theme-toggle{display:none!important;visibility:hidden!important;pointer-events:none!important}
#pc-settings-avatar{width:76px;height:76px;border-radius:50%;object-fit:cover;background:var(--gl);border:2px solid var(--line)}
'''
if 'PC_ACCOUNT_SETTINGS_V1' not in html:
    if '</style>' not in html:
        sys.exit('ERRO: não encontrei o fechamento do CSS. Nada foi salvo.')
    html = html.replace('</style>', theme_css + '\n</style>', 1)

helpers = r'''/* PC_ACCOUNT_SETTINGS_V1 helpers */
const PC_THEME_KEY="pc_theme";
function currentTheme(){try{return localStorage.getItem(PC_THEME_KEY)==="dark"?"dark":"light"}catch{return"light"}}
function applyTheme(){document.body.classList.toggle("dark",currentTheme()==="dark")}
function setTheme(value){try{localStorage.setItem(PC_THEME_KEY,value==="dark"?"dark":"light")}catch{}applyTheme();render()}
applyTheme();
async function loadAccountProfile(){if(U.profileLoading||U.profileLoaded)return;U.profileLoading=true;try{U.user=await api("/api/account/profile");U.profileLoaded=true}catch(e){U.profileError=e.message||"Não foi possível carregar sua conta."}finally{U.profileLoading=false;if(U.view==="settings")render()}}
async function saveAvatar(input){const f=input&&input.files&&input.files[0];if(!f)return;if(!f.type||!f.type.startsWith("image/")){alert("Escolha uma imagem JPG, PNG ou WEBP.");input.value="";return}if(f.size>8*1024*1024){alert("A imagem deve ter no máximo 8 MB.");input.value="";return}try{const raw=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=reject;r.readAsDataURL(f)});const img=await new Promise((resolve,reject)=>{const im=new Image();im.onload=()=>resolve(im);im.onerror=reject;im.src=raw});const scale=Math.min(1,512/Math.max(img.width,img.height));const c=document.createElement("canvas");c.width=Math.max(1,Math.round(img.width*scale));c.height=Math.max(1,Math.round(img.height*scale));c.getContext("2d").drawImage(img,0,0,c.width,c.height);const avatar=c.toDataURL("image/jpeg",0.78);if(avatar.length>800000){alert("Essa foto ficou muito grande. Escolha uma imagem menor.");return}const d=await api("/api/account/profile","POST",{avatar});U.user=d;U.profileLoaded=true;render();alert("Foto de perfil atualizada!")}catch(e){alert(e.message||"Não foi possível salvar a foto.")}finally{if(input)input.value=""}}
async function deleteAccount(){if(!confirm("Excluir sua conta permanentemente? Seus dados e produtos salvos serão apagados. Essa ação não pode ser desfeita."))return;try{await api("/api/account/delete","POST",{});try{localStorage.removeItem("pc_t")}catch{}U.user=null;U.profileLoaded=false;alert("Conta excluída com sucesso.");location.href="/"}catch(e){alert(e.message||"Não foi possível excluir a conta. Tente novamente.")}}
'''
# Replace cookie storage helpers.
ck_decl = re.compile(r'const CKK\s*=\s*"pc_ck"\s*,\s*ckGet\s*=\s*\(\)\s*=>.*?;', re.S)
new_ck_decl = '''const CKK="pc_ck",ckGet=()=>{try{const v=localStorage.getItem(CKK);if(v)return v}catch{}try{const m=document.cookie.match(/(?:^|;\\s*)pc_ck=([^;]+)/);return m?decodeURIComponent(m[1]):null}catch{return null}};'''
html, ck_n = ck_decl.subn(lambda m: new_ck_decl, html, count=1)
if ck_n != 1:
    sys.exit('ERRO: não encontrei o armazenamento do consentimento de cookies. Nenhuma alteração foi salva.')
ck_set = re.compile(r'function ckSet\(v\)\s*\{.*?\}\s*(?=function ckShow\(open\))', re.S)
new_ck_set = '''function ckSet(v){try{localStorage.setItem(CKK,v)}catch{}try{document.cookie="pc_ck="+encodeURIComponent(v)+"; Max-Age=31536000; Path=/; SameSite=Lax"}catch{}ckShow()}
'''
html, ckset_n = ck_set.subn(new_ck_set, html, count=1)
if ckset_n != 1:
    sys.exit('ERRO: não encontrei a função de salvar a escolha dos cookies. Nenhuma alteração foi salva.')

# Insert the account-settings helper functions right before the cookie dialog helpers.
if 'PC_ACCOUNT_SETTINGS_V1 helpers' not in html:
    marker = 'const CKK="pc_ck"'
    pos = html.find(marker)
    if pos < 0:
        sys.exit('ERRO: não encontrei o ponto de inserção das funções. Nenhuma alteração foi salva.')
    html = html[:pos] + helpers + '\n' + html[pos:]

# Add entry to Mais.
menu_anchor = 'mrow("go(\'plans\')","Planos e assinatura"'
if 'mrow("go(\'settings\')","Configurações da conta"' not in html:
    if menu_anchor not in html:
        sys.exit('ERRO: não encontrei a entrada Planos no menu Mais. Nenhuma alteração foi salva.')
    html = html.replace(menu_anchor, 'mrow("go(\'settings\')","Configurações da conta","Foto de perfil, aparência e segurança")}${mrow("go(\'plans\')","Planos e assinatura"', 1)

# Make settings a regular internal view, with theme + profile + delete account.
settings_view = r'''settings:()=>{if(!U.profileLoading&&!U.profileLoaded)loadAccountProfile();const u=U.user||{};const theme=currentTheme();const avatar=u.avatar||"";return backM+hd("Configurações da conta","Gerencie seu perfil, aparência e segurança")+`<div class="card" style="margin-top:14px"><b>Foto de perfil</b><div style="display:flex;align-items:center;gap:14px;margin-top:12px">${avatar?`<img id="pc-settings-avatar" src="${esc(avatar)}" alt="Foto de perfil">`:`<div id="pc-settings-avatar" style="display:grid;place-items:center;font-size:25px;color:var(--gd)">${ic("user",30)}</div>`}<div style="flex:1;min-width:0"><p class="mute sm" style="margin-bottom:8px">${esc(u.name||"Sua conta")}</p><p class="mute sm" style="margin-bottom:10px;overflow-wrap:anywhere">${esc(u.email||"")}</p><button class="btn sec" style="height:42px" onclick="$('pc-profile-file').click()">Trocar foto</button><input id="pc-profile-file" type="file" accept="image/png,image/jpeg,image/webp" style="display:none" onchange="saveAvatar(this)"></div></div>${U.profileLoading?`<p class="mute sm" style="margin-top:8px">Carregando dados da conta…</p>`:""}${U.profileError?`<p class="err" style="margin-top:8px">${esc(U.profileError)}</p>`:""}</div><div class="card"><b>Aparência</b><p class="mute sm" style="margin:4px 0 12px">Sua preferência ficará salva neste aparelho.</p><div class="row"><button class="btn ${theme==="light"?"":"sec"}" onclick="setTheme('light')">☀️ Claro</button><button class="btn ${theme==="dark"?"":"sec"}" onclick="setTheme('dark')">🌙 Escuro</button></div></div><div class="card" style="border-color:#dc2626"><b>Excluir conta</b><p class="mute sm" style="margin:4px 0 12px">Apaga permanentemente sua conta e os dados associados. Não será possível desfazer.</p><button class="btn" style="background:#dc2626" onclick="deleteAccount()">Excluir minha conta</button></div>`},
'''
if 'settings:()=>{if(!U.profileLoading' not in html:
    # Use the FAQ entry inside Object.assign(VIEWS,...) as a stable insertion point.
    assign_pos = html.find('Object.assign(VIEWS,{')
    faq_pos = html.find('faq:()=>backM+', assign_pos)
    if assign_pos < 0 or faq_pos < 0:
        sys.exit('ERRO: não encontrei as definições das telas para inserir Configurações. Nenhuma alteração foi salva.')
    html = html[:faq_pos] + settings_view + html[faq_pos:]

# Include settings in the group handled by the existing app navigation.
html, mv_n = re.subn(r'const MV=\["more","faq"', 'const MV=["more","settings","faq"', html, count=1)
if mv_n != 1:
    # If already added, the patch remains valid; otherwise do not guess about the router.
    if 'const MV=["more","settings","faq"' not in html:
        sys.exit('ERRO: não encontrei a lista de telas do menu. Nenhuma alteração foi salva.')

# Add backend account-profile endpoints and secure account deletion.
if 'PC_ACCOUNT_API_V1' not in backend:
    route_marker = '# ---------------------------------------------------------------- pagamento'
    p = backend.find(route_marker)
    if p < 0:
        # Compatible with another comment spelling, but only if the payments section exists.
        p = backend.find('# pagamento')
    if p < 0:
        sys.exit('ERRO: não encontrei o local seguro para inserir as rotas da conta. Nenhuma alteração foi salva.')
    routes = '''# PC_ACCOUNT_API_V1 — perfil, foto de perfil e exclusão de conta
@app.get("/api/account/profile")
async def get_account_profile(request: Request):
    user = await current_user(request)
    return {"name": user.get("name", ""), "email": user["email"], "avatar": user.get("avatar", "")}


@app.post("/api/account/profile")
async def update_account_profile(request: Request):
    user = await current_user(request)
    data = await body_of(request)
    avatar = str(data.get("avatar") or "")
    if len(avatar) > 800_000:
        raise HTTPException(413, "A foto ficou grande demais. Escolha uma imagem menor.")
    if avatar and not re.fullmatch(r"data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/=\\r\\n]+", avatar):
        raise HTTPException(400, "Formato de foto inválido. Use JPG, PNG ou WEBP.")
    await db.users.update_one({"id": user["id"]}, {"$set": {"avatar": avatar}})
    return {"name": user.get("name", ""), "email": user["email"], "avatar": avatar}


@app.post("/api/account/delete")
async def delete_account(request: Request):
    user = await current_user(request)
    # Se houver assinatura Stripe ativa, tente cancelá-la antes de apagar os dados.
    customer_id = user.get("stripe_customer_id")
    if customer_id and STRIPE_SECRET_KEY:
        try:
            subscriptions = stripe.Subscription.list(customer=customer_id, status="active", limit=100)
            for subscription in subscriptions.auto_paging_iter():
                stripe.Subscription.cancel(subscription.id)
        except Exception:
            log.exception("Falha ao cancelar assinatura durante exclusão da conta")
            raise HTTPException(502, "Não foi possível encerrar a assinatura agora. A conta não foi excluída; tente novamente.")
    await db.stores.delete_one({"user_id": user["id"]})
    await db.resets.delete_many({"user_id": user["id"]})
    await db.users.delete_one({"id": user["id"]})
    return {"ok": True}


'''
    backend = backend[:p] + routes + backend[p:]

# Verify expected functions/views exist before writing anything.
required = ['function setTheme(value)', 'async function saveAvatar(input)', 'async function deleteAccount()', 'settings:()=>', 'pc_ck=', 'Configurações da conta']
missing = [x for x in required if x not in html]
if missing:
    sys.exit('ERRO: validação interna falhou (' + ', '.join(missing) + '). Nenhum arquivo foi salvo.')
if 'PC_ACCOUNT_API_V1' not in backend:
    sys.exit('ERRO: rota de conta não foi preparada. Nenhum arquivo foi salvo.')

# Validate JavaScript and Python in temporary files before replacing originals.
scripts = re.findall(r'<script\b[^>]*>(.*?)</script\s*>', html, re.I | re.S)
if not scripts:
    sys.exit('ERRO: não encontrei scripts para validar. Nenhum arquivo foi salvo.')
for i, script in enumerate(scripts, 1):
    tmp = Path(f'/tmp/preocerto-patch-check-{i}.js')
    tmp.write_text(script, encoding='utf-8')
    result = subprocess.run(['node', '--check', str(tmp)], capture_output=True, text=True)
    if result.returncode:
        print(result.stderr[:3000])
        sys.exit(f'ERRO: JavaScript {i} não passou na validação. Arquivos originais mantidos.')
py_tmp = Path('/tmp/preocerto-patch-server.py')
py_tmp.write_text(backend, encoding='utf-8')
result = subprocess.run([sys.executable, '-m', 'py_compile', str(py_tmp)], capture_output=True, text=True)
if result.returncode:
    print(result.stderr[:3000])
    sys.exit('ERRO: Python do backend não passou na validação. Arquivos originais mantidos.')

backup_dir = ROOT / 'backup-melhorias'
backup_dir.mkdir(exist_ok=True)
stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
shutil.copy2(HTML, backup_dir / f'index-antes-configuracoes-{stamp}.bak')
shutil.copy2(BACKEND, backup_dir / f'server-antes-configuracoes-{stamp}.bak')
HTML.write_text(html, encoding='utf-8')
BACKEND.write_text(backend, encoding='utf-8')
print('SUCESSO: alterações aplicadas e validadas.')
print('Incluído: consentimento de cookies persistente, botão da lua oculto, tema claro/escuro, foto de perfil e exclusão de conta com confirmação.')
print('Backups salvos em backup-melhorias/.')
print('Atenção: após publicar, teste primeiro em uma conta de teste antes de usar Excluir minha conta.')
