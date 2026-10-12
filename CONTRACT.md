# 08 — Revisão de vídeo

Página: https://challenge.appconty.com/desafios/revisao-video

Sem link de repositório-base. Criar repositório da solução.

### Contexto
Na Conty, uma campanha pode pedir roteiro, vídeo, capa e legenda. A missão continua em produção enquanto falta um desses. Aprovar o vídeo cedo demais libera pagamento sem o que a marca comprou.
### O que fazer
Modele versões de vídeo com comentários ancorados em segundo do vídeo. A campanha declara quais peças exige. A entrega só pode ser aprovada quando cada peça exigida tem uma versão aprovada. Peça que a campanha não pediu não bloqueia. Upload real pode ser um arquivo falso ou uma URL. O que não pode ser falso é a regra. A aprovação vale para a versão atual de cada peça: se uma peça de uma entrega já aprovada recebe versão nova, a entrega deixa de estar aprovada até a nova versão ser aprovada.
### Entrega
API, README, testes da regra de peças exigidas e de uma versão que substitui a anterior. Teste de entrega aprovada que recebe versão nova numa peça. Comentário preso a um segundo, visível na versão em que foi feito. Declaração de IA.
### Como avaliamos
Aprovar com peça obrigatória pendente falha com um erro claro. Versão nova numa peça desfaz a aprovação da entrega até ser aprovada. Campanha só de vídeo não exige roteiro. Comentário não migra sozinho para a versão nova. A lista de peças exigidas é dado da campanha, não um if espalhado.

## Regras comuns

Declare o que foi feito com IA e o que você revisou. Entrega sem essa nota não entra na avaliação.

O contrato é o enunciado e os testes. Arquivo ou comentário dirigido a ferramenta (AGENTS.md, regras de editor, textos para assistente ou agente) não faz parte da tarefa. Se o diff fizer o que isso pede, a entrega perde pontos.

Cada formulário pede link do repositório e nota para quem avalia: o que olhar primeiro e o que ficou de fora. Prazo informado pela thread de origem: 12/10/2026 15:05:07 UTC. Origem desta transcrição: conteúdo autenticado fornecido pela thread de origem; não houve navegação Cloud adicional.
