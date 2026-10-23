# Preço Certo

Site (front em `public/`) + API (`backend/`, FastAPI + MongoDB).
Login com e-mail/senha, recuperação de senha por e-mail (Resend) e assinatura recorrente via SyncPay (Pix Automático e cartão).

| Parte | Onde roda |
|---|---|
| Site (`public/`) | Vercel |
| API (`backend/`) | Render |
| Banco | MongoDB Atlas |
| E-mails | Resend |
| Pagamento | SyncPay |

## Como funciona
- O teste grátis (7 dias) começa na criação da conta. Depois, só com plano Completo (R$ 19,90/mês).
- O plano e a data do teste ficam **no servidor**; o navegador não consegue se dar o plano pago.
- A SyncPay avisa a API por webhook quando a assinatura começa, muda ou é cancelada.
- Cancelar: o app solicita o cancelamento da assinatura pela API SyncPay.
- Apagar conta: em **Mais → Configurações da conta → Apagar conta** (pede a senha). A API (`POST /api/account/delete`) cancela a assinatura na SyncPay e apaga usuário, dados da loja, tokens de senha e sugestões. Funciona mesmo com o teste vencido.
- Atualizar a página mantém a tela atual: cada tela tem uma URL (`/produtos`, `/estoque`, `/mais/configuracoes`, `/produto/ID/2` etc.). O `vercel.json` manda toda rota que não é `/api` para o `index.html`.

## Passo a passo (nesta ordem)

### 1. MongoDB Atlas
1. Crie um cluster gratuito e um usuário de banco (anote usuário e senha).
2. Em **Network Access**, libere `0.0.0.0/0` (o Render não tem IP fixo).
3. Em **Connect → Drivers**, copie a string `mongodb+srv://...` → será o `MONGO_URL`.

### 2. Login com Google
1. Google Cloud Console → crie um projeto → **APIs e serviços → Tela de consentimento OAuth** (tipo Externo) e, ao terminar, **publique o app** (senão só você consegue entrar).
2. **Credenciais → Criar credenciais → ID do cliente OAuth → Aplicativo da Web**.
3. Em **Origens JavaScript autorizadas** coloque a URL do site (ex.: `https://preco-certo.vercel.app`) e, depois, o domínio próprio.
4. Copie o ID do cliente → `GOOGLE_CLIENT_ID`. (Não precisa do "segredo do cliente".)

### 3. Resend (e-mail de "esqueci a senha")
1. Crie uma API key → `RESEND_API_KEY`.
2. Para enviar a qualquer pessoa é preciso **verificar um domínio** no Resend e usar um remetente dele em `MAIL_FROM`.
   Sem domínio, o Resend só entrega para o e-mail dono da conta (serve para testar).

### 4. SyncPay (Pix Automático e cartão recorrente)
1. Na SyncPay, gere `client_id`, `client_secret` e a chave pública `pk_live_...` (a chave pública só é usada no navegador para tokenizar cartão).
2. Crie dois planos recorrentes no painel/API: um com `billing_method: pix_automatico` e outro com `billing_method: credit_card`, ambos com o preço mensal desejado (o padrão do site é R$ 19,90). Copie os tokens dos planos.
3. Para Pix Automático, confirme com a SyncPay que o recurso está habilitado para sua conta. A API retorna erro se não estiver liberado.
4. No Render, configure `SYNCPAY_CLIENT_ID`, `SYNCPAY_CLIENT_SECRET`, `SYNCPAY_PUBLIC_KEY`, `SYNCPAY_PLAN_PIX_TOKEN`, `SYNCPAY_PLAN_CARD_TOKEN` e `SYNCPAY_WEBHOOK_SECRET`. Nunca coloque `client_secret` no HTML, na Vercel ou no GitHub.
5. Cadastre o webhook para eventos de assinatura apontando para `https://SEU-SERVICO.onrender.com/api/syncpay/webhook`. Confirme com o suporte SyncPay o header/algoritmo de assinatura dos eventos de assinatura antes de produção; o código valida `X-SyncPay-Signature` com HMAC-SHA256.

### 5. API no Render
1. render.com → **New → Blueprint** → escolha este repositório (ele lê o `render.yaml`).
2. Preencha as variáveis (veja `backend/.env.example`). `SITE_URL` é a URL do site na Vercel, sem barra no fim.
3. Quando subir, abra `https://SEU-SERVICO.onrender.com/api/health` → deve responder `{"ok":true}`.
   No plano grátis o Render "dorme" após alguns minutos parado e a 1ª chamada demora ~1 minuto.

### 6. Webhook SyncPay
1. Cadastre os eventos `assinatura_ativada`, `assinatura_reativada`, `assinatura_renovada`, `assinatura_em_atraso`, `assinatura_suspensa` e `assinatura_cancelada`.
2. URL: `https://SEU-SERVICO.onrender.com/api/syncpay/webhook`.
3. Configure o segredo do webhook em `SYNCPAY_WEBHOOK_SECRET` e confirme o padrão de assinatura no painel/suporte SyncPay.

### 7. Site na Vercel
1. Abra `vercel.json` e troque `https://SEU-BACKEND.onrender.com` pela URL real do Render (nas duas chaves). Comando:
   `sed -i 's|SEU-BACKEND.onrender.com|NOME-REAL.onrender.com|' vercel.json`
2. Suba para o GitHub; na Vercel, **Add New → Project** com este repositório (sem configurar build; o `vercel.json` já cuida).
3. Volte ao passo 2 (Google) e ao `SITE_URL` do Render se a URL final for diferente da usada.

## Teste antes de ir para valer
1. Configure primeiro credenciais e tokens de planos em ambiente de teste, se disponíveis para sua conta.
2. Crie uma conta de usuário e teste o fluxo de assinatura com Pix Automático.
3. Teste cartão com os dados de teste fornecidos pela SyncPay. Os dados do cartão são tokenizados no navegador e não devem passar pelo backend nem ser armazenados.
4. Confirme que o webhook ativa o plano somente após `assinatura_ativada` e que cancelamento/suspensão remove o acesso.
5. Antes de cobrar clientes reais, confirme com o suporte SyncPay o formato exato da assinatura dos webhooks de recorrência e teste os eventos reais.

**Importante:** a integração precisa das credenciais, dos tokens dos dois planos e do segredo do webhook configurados no Render. O ZIP não contém nem deve conter chaves reais.
