# StorageAudit

### Auditoria inteligente e análise de armazenamento

**StorageAudit** é uma ferramenta universal de auditoria e análise de armazenamento, desenvolvida em **Python**, projetada para oferecer visibilidade detalhada sobre a utilização de espaço em discos, diretórios, partições montadas e compartilhamentos de rede.

A solução automatiza o levantamento de arquivos e pastas, identifica os principais consumidores de armazenamento e gera relatórios HTML interativos, com indicadores, rankings, gráficos e comparações históricas.

Seu objetivo é auxiliar profissionais de Tecnologia da Informação, administradores de sistemas e equipes de infraestrutura na **gestão, no monitoramento e no planejamento da capacidade de armazenamento**, fornecendo informações organizadas para apoiar decisões técnicas e operacionais.

---

## Principais funcionalidades

### Análise de armazenamento
- Identificação automática das unidades disponíveis no sistema.
- Seleção interativa da unidade ou diretório que será analisado.
- Suporte a caminhos locais e compartilhamentos de rede (UNC).
- Varredura detalhada de pastas, subpastas e arquivos.
- Identificação do tamanho, localização e data de modificação dos arquivos.
- Registro de erros de acesso e restrições de permissão.

### Dashboard interativo
- Geração automática de relatórios em HTML.
- Indicadores de consumo e utilização de armazenamento.
- Rankings das maiores pastas e dos maiores arquivos.
- Representação gráfica da distribuição de espaço.
- Explorador interativo com navegação entre diretórios.
- Pesquisa e filtros para localização de arquivos e pastas.

### Histórico e comparações
- Armazenamento persistente dos resultados em banco de dados SQLite.
- Registro de múltiplas análises realizadas ao longo do tempo.
- Comparação entre varreduras do mesmo caminho.
- Identificação de arquivos novos, removidos ou com alterações de tamanho.
- Indicadores de crescimento e redução de armazenamento.
- Gráficos de evolução histórica para acompanhamento do consumo.

### Exportação de resultados
- Relatórios HTML independentes, visualizáveis em navegadores.
- Exportação opcional do detalhamento completo em CSV.
- Histórico estruturado para consultas e comparações posteriores.

---

## Requisitos

- Python 3.9 ou superior.
- Windows 10/11 ou Windows Server.
- Permissões de leitura nos diretórios analisados.
- Acesso à rede, quando necessário, para auditoria de compartilhamentos remotos.

**Não é necessária a instalação de bibliotecas Python de terceiros.**

## Instalação e execução

1. Faça o download dos arquivos disponíveis neste repositório.
2. Extraia ou organize os arquivos em um mesmo diretório.
3. Execute `executar_auditoria_armazenamento.bat` com dois cliques.
4. Selecione uma unidade disponível ou informe manualmente o caminho desejado.
5. Aguarde a conclusão da varredura.
6. Abra o relatório HTML gerado para consultar os resultados.

### Execução alternativa

A ferramenta também pode ser executada diretamente pelo terminal:

```bash
python auditoria_armazenamento_universal.py
```

Para analisar um diretório específico:

```bash
python auditoria_armazenamento_universal.py --raiz "D:\Dados"
```

Para analisar um compartilhamento de rede:

```bash
python auditoria_armazenamento_universal.py --raiz "\\servidor\compartilhamento"
```

### Parâmetros adicionais

| Parâmetro | Finalidade |
|---|---|
| `--listar-unidades` | Lista as unidades disponíveis. |
| `--top 25` | Define a quantidade de itens exibidos nos rankings. |
| `--limite-html 0` | Inclui todos os arquivos no detalhamento HTML. |
| `--csv` | Exporta os resultados para CSV. |
| `--historico` | Define o arquivo do banco de dados histórico. |
| `--saida` | Personaliza o nome do relatório HTML. |
| `--silencioso` | Oculta mensagens intermediárias de progresso. |

---

## Segurança e integridade

O StorageAudit foi desenvolvido para realizar **varreduras somente de leitura**, sem alterar, mover ou excluir os arquivos presentes nos diretórios analisados.

A ferramenta não exige conexão com serviços externos para gerar seus relatórios e mantém os dados históricos localmente.

Os relatórios podem conter informações sobre a estrutura dos diretórios e nomes de arquivos. Portanto, recomenda-se armazená-los em locais com acesso controlado.

## Considerações técnicas

- O tamanho apresentado corresponde ao tamanho lógico dos arquivos, podendo diferir do espaço físico efetivamente ocupado no disco.
- Links simbólicos e pontos de junção não são percorridos, evitando ciclos e contagens duplicadas.
- Diretórios sem permissão de leitura podem gerar resultados incompletos.
- Arquivos movidos ou renomeados são identificados nas comparações como remoções e novas inclusões.
- O histórico é organizado por caminho analisado, permitindo acompanhar diferentes unidades e diretórios de maneira independente.
- Em volumes extensos, o tempo de execução e o tamanho dos relatórios podem aumentar conforme a quantidade de arquivos.

---

## Tecnologias utilizadas

- **Python:** processamento, varredura e consolidação dos dados.
- **SQLite:** persistência do histórico de auditorias.
- **HTML5 e CSS3:** estrutura e apresentação dos relatórios.
- **JavaScript:** navegação, pesquisa e interatividade dos dashboards.
- **Windows Batch (.bat):** inicialização simplificada da aplicação.

## Objetivo do projeto

Promover uma abordagem mais eficiente, acessível e orientada a dados para a administração de armazenamento, transformando informações técnicas sobre arquivos e diretórios em indicadores visuais e históricos que facilitem a identificação de oportunidades de otimização e o planejamento da infraestrutura.

---

**StorageAudit — Visibilidade, controle e inteligência na gestão de armazenamento.**
