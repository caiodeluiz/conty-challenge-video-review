# Revisão de vídeo

API local para versionar as peças de uma entrega, aprovar versões e manter comentários de vídeo presos à versão e ao segundo em que foram escritos. A campanha define os tipos exigidos. A aprovação depende das versões atuais dessas peças e, após a primeira aprovação, também da revisão de qualquer versão adicionada, inclusive a primeira versão de uma peça opcional.

## Executar

Requer Python 3.12. Usa somente a biblioteca padrão e cria o banco persistente no caminho informado.

```sh
python3 server.py --database data/video_review.sqlite3 --port 8765
python3 -m unittest discover -s tests -v
```

O servidor escuta somente em `127.0.0.1`. Não há upload: `asset_ref` aceita uma referência fictícia ou URL informada pelo cliente.

## Fluxo da API

`POST /campaigns` declara as peças obrigatórias. Os nomes permitidos são `roteiro`, `video`, `capa` e `legenda`.

```sh
curl -s http://127.0.0.1:8765/campaigns -H 'content-type: application/json' \
  -d '{"campaign_id":"cmp-1","name":"Campanha","required_pieces":["video","legenda"]}'
curl -s http://127.0.0.1:8765/campaigns/cmp-1/deliveries -H 'content-type: application/json' \
  -d '{"delivery_id":"del-1"}'
curl -s http://127.0.0.1:8765/deliveries/del-1/pieces/video/versions -H 'content-type: application/json' \
  -d '{"version_id":"vid-v1","asset_ref":"fake://video/v1","submitted_by":"creator-1","duration_seconds":42.5}'
curl -s -X POST http://127.0.0.1:8765/deliveries/del-1/pieces/video/versions/vid-v1/approve \
  -H 'content-type: application/json' -d '{"reviewer_id":"brand-1"}'
curl -s -X POST http://127.0.0.1:8765/deliveries/del-1/pieces/legenda/versions -H 'content-type: application/json' \
  -d '{"version_id":"cap-v1","asset_ref":"fake://caption/v1","submitted_by":"creator-1"}'
curl -s -X POST http://127.0.0.1:8765/deliveries/del-1/pieces/legenda/versions/cap-v1/approve \
  -H 'content-type: application/json' -d '{"reviewer_id":"brand-1"}'
curl -s -X POST http://127.0.0.1:8765/deliveries/del-1/approve \
  -H 'content-type: application/json' -d '{"reviewer_id":"brand-1"}'
curl -s -X POST http://127.0.0.1:8765/deliveries/del-1/pieces/video/versions/vid-v1/comments \
  -H 'content-type: application/json' \
  -d '{"comment_id":"com-1","reviewer_id":"brand-1","seconds":12.5,"body":"Cortar a pausa"}'
```

Os endpoints de leitura são `GET /campaigns/{campaign_id}`, `GET /deliveries/{delivery_id}`, `GET /deliveries/{delivery_id}/pieces/{piece}/versions/{version_id}` e `GET /deliveries/{delivery_id}/pieces/video/versions/{version_id}/comments`. `GET /deliveries/{delivery_id}` inclui peças exigidas, versões atuais e antigas e eventos da aprovação. Erros de validação usam 400, entidades ausentes 404 e conflito de regra/ID 409.

## Decisões e limites

- `required_pieces` continua sendo exatamente a lista configurada pela campanha. Uma peça opcional ausente não bloqueia a aprovação inicial. Ausência difere de alterar uma entrega já aprovada: depois da primeira aprovação, adicionar qualquer versão cria pendência, inclusive a primeira capa opcional.
- Esta regra resolve uma ambiguidade do contrato por decisão de proteção de marca autorizada pelo usuário: a marca precisa revisar toda versão adicionada depois da primeira aprovação, mesmo opcional e mesmo enquanto outra revisão já mantém a entrega suspensa. Não apresentamos essa decisão como regra pública comprovada.
- Cada versão adicionada após a primeira aprovação acrescenta um evento `approval_invalidated` com o tipo de peça e a versão corrente que precisa de aprovação. Revisões pendentes são reduzidas à versão mais recente de cada peça desde o último evento `approved`. Aprovar uma peça diferente não limpa essas pendências; `approve_delivery` responde 409 enquanto qualquer versão adicionada continuar pendente. Ao aprovar as versões atuais pendentes e satisfazer todas as peças exigidas, o serviço registra um novo evento de aprovação.
- Comentários só podem ser adicionados a versões de `video`, inclusive versões antigas. Eles guardam `version_id`; não são copiados ao criar versão nova. `seconds` deve ser finito e não negativo. Se a versão informa `duration_seconds`, o instante exatamente igual à duração é aceito; valores acima dela são rejeitados.
- `asset_ref` é apenas texto; não há upload, inspeção de URL, autenticação, autorização de reviewer, notificação ou integração com serviços reais. Os IDs e autores vêm do cliente local.
- Os campos de texto utilizados pelo serviço precisam ser codificáveis em UTF-8; surrogate isolado é rejeitado com 400 antes da escrita. Unicode válido e emoji são preservados; o conteúdo dos comentários mantém seus espaços originais.
- Cada escrita usa `BEGIN IMMEDIATE`; versões, aprovações, eventos e comentários são mantidos no SQLite. Campanhas não têm endpoint de edição nesta demo.
- Não há migração retroativa dos dados antigos: a invalidação depende dos eventos registrados a partir desta regra. Inclusões ou substituições opcionais realizadas antes desta atualização não são invalidadas automaticamente.
- Para `V` versões, carregar/selecionar versões atuais custa `O(V)`; a visão também ordena a lista histórica em Python, que custa `O(V log V)`, e mantém `O(V)` dados em memória. A verificação das peças obrigatórias custa `O(R)`.
- Para `E` eventos desta entrega, a aplicação reduz eventos posteriores à última aprovação em `O(E)` tempo e mantém até `O(E)` alvos/resultados em memória. A consulta SQL filtra por `delivery_id` e ordena por `event_no`, mas não há índice composto com `delivery_id` como primeira coluna; SQLite pode examinar até `E_total` eventos do banco para retornar os `E` relevantes. O planner pode aproveitar a ordem da chave primária `event_no` ou usar uma ordenação temporária, que acrescentaria até `O(E log E)`; não prometemos custo SQL end-to-end `O(E)`.
- A leitura ordenada dos `C` comentários de uma versão usa índice por versão e custa aproximadamente `O(log C + C)` com `O(C)` resultados em memória.

## Para quem avalia

Primeiro olhe `tests/test_video_review.py`, depois `service.py`: os testes cobrem campanha só de vídeo sem capa, vídeo com capa opcional incluída, primeira inclusão opcional após aprovação, substituição de opcionais, bloqueio de aprovação enquanto há revisão pendente, versões antigas após restart, comentários presos à versão e limite de segundos. Ficaram de fora upload real, autenticação, operação em produção e uma política para edição de campanhas após a criação.
