# Preço Certo

Site (front em `public/`) + API (`backend/`, FastAPI + MongoDB).
Login com e-mail/senha, recuperação de senha por e-mail (Resend) e assinatura recorrente via SyncPay (Pix Automático; cartão recorrente já está no código e liga sozinho quando as variáveis do cartão existirem).

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
- Cancelar: o app solicita o cancelamento da assinatura pela API SyncPay. Se o cliente gerou o Pix e desistiu, a tela do plano mostra "Assinatura aguardando confirmação" com o botão "Cancelar tentativa", e ele pode assinar de novo.
- Trocar a senha (link do e-mail) encerra as sessões que já estavam abertas.
- Apagar conta: em **Mais → Configurações da conta → Apagar conta** (pede a senha). A API (`POST /api/account/delete`) cancela a assinatura na SyncPay e apaga usuário, dados da loja, tokens de senha e sugestões. Funciona mesmo com o teste vencido.
- Atualizar a página mantém a tela atual: cada tela tem uma URL (`/produtos`, `/estoque`, `/mais/configuracoes`, `/produto/ID/2` etc.). O `vercel.json` manda toda rota que não é `/api` para o `index.html`.

## Passo a passo (nesta ordem)

### 1. MongoDB Atlas
1. Crie um cluster gratuito e um usuário de banco (anote usuário e senha).
2. Em **Network Access**, libere `0.0.0.0/0` (o Render não tem IP fixo).
3. Em **Connect → Drivers**, copie a string `mongodb+srv://...` → será o `MONGO_URL`.

### 2. Resend (e-mail de "esqueci a senha")
1. Crie uma API key → `RESEND_API_KEY`.
2. Para enviar a qualquer pessoa é preciso **verificar um domínio** no Resend e usar um remetente dele em `MAIL_FROM`.
   Sem domínio, o Resend só entrega para o e-mail dono da conta (serve para testar).

### 3. SyncPay (Pix Automático; cartão recorrente é opcional)
1. Na SyncPay, gere `client_id`, `client_secret` e a chave pública `pk_live_...` (a chave pública só é usada no navegador para tokenizar cartão).
2. Crie o plano recorrente com `billing_method: pix_automatico` e o preço mensal desejado (o padrão do site é R$ 19,90). Copie o token do plano. O plano de cartão (`billing_method: credit_card`) é opcional: enquanto `SYNCPAY_PLAN_CARD_TOKEN` e `SYNCPAY_PUBLIC_KEY` não existirem no Render, a opção de cartão não aparece no app.
3. Para Pix Automático, confirme com a SyncPay que o recurso está habilitado para sua conta. A API retorna erro se não estiver liberado.
4. No Render, configure `SYNCPAY_CLIENT_ID`, `SYNCPAY_CLIENT_SECRET`, `SYNCPAY_PLAN_PIX_TOKEN` e `SYNCPAY_WEBHOOK_SECRET`. Só para o cartão: `SYNCPAY_PUBLIC_KEY` e `SYNCPAY_PLAN_CARD_TOKEN`. Nunca coloque `client_secret` no HTML, na Vercel ou no GitHub.
5. Cadastre o webhook para eventos de assinatura apontando para `https://SEU-SERVICO.onrender.com/api/syncpay/webhook`. Confirme com o suporte SyncPay o header/algoritmo de assinatura dos eventos de assinatura antes de produção; o código valida `X-SyncPay-Signature` com HMAC-SHA256.

### 4. API no Render
1. render.com → **New → Blueprint** → escolha este repositório (ele lê o `render.yaml`).
2. Preencha as variáveis (veja `backend/.env.example`). `SITE_URL` é a URL do site na Vercel, sem barra no fim.
3. Quando subir, abra `https://SEU-SERVICO.onrender.com/api/health` → deve responder `{"ok":true}`.
   No plano grátis o Render "dorme" após alguns minutos parado e a 1ª chamada demora ~1 minuto.

### 5. Webhook SyncPay
1. Cadastre os eventos `assinatura_ativada`, `assinatura_reativada`, `assinatura_renovada`, `assinatura_em_atraso`, `assinatura_suspensa` e `assinatura_cancelada`.
2. URL: `https://SEU-SERVICO.onrender.com/api/syncpay/webhook`.
3. Configure o segredo do webhook em `SYNCPAY_WEBHOOK_SECRET` e confirme o padrão de assinatura no painel/suporte SyncPay.

### 6. Site na Vercel
1. Abra `vercel.json` e confira que a URL em `destination` (`/api/:path*`) é a do seu serviço no Render. Se mudar o nome do serviço, troque aqui também.
2. Suba para o GitHub; na Vercel, **Add New → Project** com este repositório (sem configurar build; o `vercel.json` já cuida).
3. Se a URL final do site for diferente da usada, atualize o `SITE_URL` no Render (sem barra no fim). Ele é usado no link do e-mail de "esqueci a senha".

## Teste antes de ir para valer
1. Configure primeiro credenciais e tokens de planos em ambiente de teste, se disponíveis para sua conta.
2. Crie uma conta de usuário e teste o fluxo de assinatura com Pix Automático.
3. Teste cartão com os dados de teste fornecidos pela SyncPay. Os dados do cartão são tokenizados no navegador e não devem passar pelo backend nem ser armazenados.
4. Confirme que o webhook ativa o plano somente após `assinatura_ativada` e que cancelamento/suspensão remove o acesso.
5. Antes de cobrar clientes reais, confirme com o suporte SyncPay o formato exato da assinatura dos webhooks de recorrência e teste os eventos reais.

**Importante:** a integração precisa das credenciais, dos tokens dos dois planos e do segredo do webhook configurados no Render. O ZIP não contém nem deve conter chaves reais.
