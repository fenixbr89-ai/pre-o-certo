# Preço Certo

Site (front em `public/`) + API (`backend/`, FastAPI + MongoDB).
Login com e-mail/senha e com Google, recuperação de senha por e-mail (Resend) e assinatura mensal (Stripe).

| Parte | Onde roda |
|---|---|
| Site (`public/`) | Vercel |
| API (`backend/`) | Render |
| Banco | MongoDB Atlas |
| E-mails | Resend |
| Pagamento | Stripe |

## Como funciona
- O teste grátis (7 dias) começa na criação da conta. Depois, só com plano Completo (R$ 19,90/mês).
- O plano e a data do teste ficam **no servidor**; o navegador não consegue se dar o plano pago.
- O Stripe avisa a API (webhook) quando a assinatura começa, muda ou é cancelada.
- Cancelar/gerenciar: o botão leva ao portal do Stripe.

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

### 4. Stripe (modo teste primeiro)
1. Painel em **modo de teste** → Desenvolvedores → Chaves de API → `STRIPE_SECRET_KEY` (`sk_test_...`).
2. **Configurações → Faturamento → Portal do cliente** → ative e permita **cancelar assinatura**.
3. O webhook é criado no passo 6, depois que a API estiver no ar.

### 5. API no Render
1. render.com → **New → Blueprint** → escolha este repositório (ele lê o `render.yaml`).
2. Preencha as variáveis (veja `backend/.env.example`). `SITE_URL` é a URL do site na Vercel, sem barra no fim.
3. Quando subir, abra `https://SEU-SERVICO.onrender.com/api/health` → deve responder `{"ok":true}`.
   No plano grátis o Render "dorme" após alguns minutos parado e a 1ª chamada demora ~1 minuto.

### 6. Webhook do Stripe
1. Stripe → Desenvolvedores → **Webhooks → Adicionar endpoint**.
2. URL: `https://SEU-SERVICO.onrender.com/api/stripe/webhook` (direto no Render, não pela Vercel).
3. Eventos: `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, `customer.subscription.deleted`.
4. Copie o **Segredo de assinatura** (`whsec_...`) → variável `STRIPE_WEBHOOK_SECRET` no Render → redeploy.

### 7. Site na Vercel
1. Abra `vercel.json` e troque `https://SEU-BACKEND.onrender.com` pela URL real do Render (nas duas chaves). Comando:
   `sed -i 's|SEU-BACKEND.onrender.com|NOME-REAL.onrender.com|' vercel.json`
2. Suba para o GitHub; na Vercel, **Add New → Project** com este repositório (sem configurar build; o `vercel.json` já cuida).
3. Volte ao passo 2 (Google) e ao `SITE_URL` do Render se a URL final for diferente da usada.

## Teste antes de ir para valer
1. Crie conta com e-mail e senha; saia e entre de novo.
2. "Esqueci minha senha" → chegou o e-mail? → o link cria nova senha?
3. "Continuar com o Google".
4. Assinar com o cartão de teste `4242 4242 4242 4242` (data futura, CVC qualquer) → volta ao site com o plano Completo ativo.
5. Plano e assinatura → Cancelar → portal do Stripe → o plano volta a "grátis" quando o Stripe avisar.

Para cobrar de verdade: troque para as chaves `sk_live_...`, crie o webhook no modo **ativo** (novo `whsec_`) e refaça o passo do portal no modo ativo.

## Pix
O Stripe aqui cobra por cartão. Para aceitar Pix depois, o caminho é somar Mercado Pago ou Asaas no endpoint `/api/checkout` (não está incluído).
