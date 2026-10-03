name: Analisador Diário de Músicas - Cifra Club

on:
  schedule:
    # Roda às 11:30 UTC (08:30 no Horário de Brasília) para o Brasil
    - cron: '30 11 * * *'
    # Roda às 13:11 UTC (10:11 no Horário de Brasília) para o Hispam
    - cron: '11 13 * * *'
  workflow_dispatch:
    # Opção pra escolher o que rodar manualmente
    inputs:
      regiao:
        description: 'Qual bloco processar? (br, hispam, all)'
        required: true
        default: 'all'

jobs:
  analisar_dados:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    # ⚡ FORÇA O SERVIDOR DO GITHUB A USAR O HORÁRIO DO BRASIL
    env:
      TZ: "America/Sao_Paulo"

    steps:
      - name: Checkout do repositório
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Configurar o Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Instalar dependências
        run: pip install requests

      - name: Definir Alvo da Análise
        id: set_target
        run: |
          # Checa qual foi o gatilho que iniciou o robô e define o alvo
          if [[ "${{ github.event_name }}" == "workflow_dispatch" ]]; then
            echo "TARGET=${{ github.event.inputs.regiao }}" >> $GITHUB_ENV
          elif [[ "${{ github.event.schedule }}" == "30 11 * * *" ]]; then
            echo "TARGET=br" >> $GITHUB_ENV
          elif [[ "${{ github.event.schedule }}" == "11 13 * * *" ]]; then
            echo "TARGET=hispam" >> $GITHUB_ENV
          else
            echo "TARGET=all" >> $GITHUB_ENV
          fi

      - name: Verificar se já rodou hoje (trava contra rodada manual)
        id: check_ja_rodou
        run: |
          # ⚡ Se você já processou o dia de hoje na mão (subindo os arquivos
          # pelo "Add files via upload", por exemplo), esse robô não deve rodar
          # sozinho no horário programado e pisar em cima do que você já fez.
          # Só vale pra disparo AGENDADO — se você aperta "Run workflow" na
          # mão, sempre roda (é o seu botão de forçar).
          HOJE=$(date +%F)

          case "${{ env.TARGET }}" in
            br) REGIOES="br" ;;
            hispam) REGIOES="hispam" ;;
            all) REGIOES="br hispam" ;;
            *) REGIOES="${{ env.TARGET }}" ;;
          esac

          PULAR=false
          if [[ "${{ github.event_name }}" == "schedule" ]]; then
            TODAS_JA_EXISTEM=true
            for r in $REGIOES; do
              if [[ ! -f "historico_dados/$r/dados_${HOJE}.json" ]]; then
                TODAS_JA_EXISTEM=false
                break
              fi
            done
            if [[ "$TODAS_JA_EXISTEM" == "true" ]]; then
              PULAR=true
            fi
          fi

          echo "PULAR=$PULAR" >> $GITHUB_ENV
          if [[ "$PULAR" == "true" ]]; then
            echo "⏭️  Dia $HOJE já tem dados pra [$REGIOES] (rodada manual anterior). Pulando a rodada agendada."
          else
            echo "▶️  Seguindo com a rodada normal pra [$REGIOES]."
          fi

      - name: Rodar script de análise
        # Repassa o alvo (br, hispam ou all) para o Python
        # e o User-Agent liberado pelo backend (guardado como secret)
        if: env.PULAR != 'true'
        env:
          CIFRA_USER_AGENT: ${{ secrets.CIFRA_USER_AGENT }}
        run: python analisador.py ${{ env.TARGET }}

      - name: Salvar alterações no GitHub (Git Puro)
        if: env.PULAR != 'true'
        run: |
          git config --global user.name "github-actions[bot]"
          git config --global user.email "41898282+github-actions[bot]@users.noreply.github.com"

          # Mapeia todas as subpastas e os arquivos dinâmicos de cada região (*.md e *.json)
          git add historico_dados/ historico_relatorios/ relatorio_diario_*.md dados_dashboard_*.json || true

          if ! git diff --cached --quiet; then
            git commit -m "📊 Histórico, relatórios e dados do painel updated (${{ env.TARGET }})"
            git pull origin main --rebase -X theirs
            git push origin main
          else
            echo "Nenhuma mudança nos dados estruturados detectada hoje."
          fi
