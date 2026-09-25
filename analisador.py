import requests
import json
import re
import os
import glob
import sys
import traceback
from datetime import datetime
from urllib.parse import urlparse

# Configurações Gerais
PASTA_DADOS = "historico_dados"
PASTA_RELATORIOS = "historico_relatorios"
MARGEM_OSCILACAO = 2

# Mapeamento de Regiões — a fonte é a página pública "Explorar > Músicas"
# (aba "Em alta na semana" / "En tendencia esta semana"):
#   BR:     https://www.cifraclub.com.br/explorar/musicas/
#   HISPAM: https://www.cifraclub.com/explorar/musicas/ com o site em espanhol
#           (es-ES, o que o site guarda no cookie "locale=es")
#
# O robô monta a lista EXATAMENTE como o site monta quando a pessoa rola a
# página (mesma lógica do código do próprio site):
#   1. As 50 primeiras vêm prontas no HTML da página ("initialResults").
#   2. As próximas levas vêm da API, uma página de 50 por vez (_page=2, 3, 4…),
#      com o mesmo endereço que o site usa.
#   3. Cada leva é emendada no fim da lista. Se uma música já apareceu antes
#      (mesmo id), o site não mostra de novo — fica valendo a primeira vez.
#   4. A numeração (01, 02, 03…) é a ordem final da lista.
# Como o site pode esconder repetições, pode ser preciso ir além da página 20
# pra chegar nas 1000 que aparecem na tela — o robô continua carregando levas
# até ter 1000.
API_EXPLORAR = "https://solr.sscdn.co/cifraclub-explore/v1/songs"
TAMANHO_TOP = 1000
MAX_PAGINAS = 60  # só uma trava de segurança pra não rodar pra sempre

REGIOES = {
    "br": {
        "nome": "Brasil",
        "pagina": "https://www.cifraclub.com.br/explorar/musicas/",
        "dominio": "https://www.cifraclub.com.br",
        "idioma": "pt",
        "cookies": {"locale": "pt"},
        "accept_language": "pt-BR,pt;q=0.9",
    },
    "hispam": {
        "nome": "Hispam",
        "pagina": "https://www.cifraclub.com/explorar/musicas/",
        "dominio": "https://www.cifraclub.com",
        "idioma": "es",
        "cookies": {"locale": "es"},
        "accept_language": "es-ES,es;q=0.9",
    },
}

def headers_padrao(config, accept):
    return {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36',
        'Accept': accept,
        'Accept-Language': config['accept_language'],
        'Referer': config['pagina'],
    }

def ler_primeira_leva_do_html(config):
    """Baixa a página Explorar e lê as 50 músicas que o site já entrega no HTML."""
    response = requests.get(
        config['pagina'],
        headers=headers_padrao(config, 'text/html,application/xhtml+xml'),
        cookies=config['cookies'],
        timeout=30,
    )
    response.raise_for_status()
    html = response.text

    # O Next.js manda os dados da página em pedaços: self.__next_f.push([1,"..."])
    payload = ""
    for trecho in re.findall(r'self\.__next_f\.push\((\[.*?\])\)</script>', html, flags=re.S):
        try:
            item = json.loads(trecho)
        except ValueError:
            continue
        if len(item) > 1 and isinstance(item[1], str):
            payload += item[1]

    marcador = '"initialResults":'
    inicio = payload.find(marcador)
    if inicio < 0:
        raise RuntimeError(f"Não achei a lista inicial (initialResults) no HTML de {config['pagina']}")
    resultados, _ = json.JSONDecoder().raw_decode(payload[inicio + len(marcador):])

    # Confere se a página veio no idioma certo (ex.: HISPAM precisa estar em espanhol)
    lang = re.search(r'<html[^>]*\blang="([^"]+)"', html)
    if lang and not lang.group(1).lower().startswith(config['idioma']):
        raise RuntimeError(f"Página veio no idioma '{lang.group(1)}', esperado '{config['idioma']}'")

    return resultados

def buscar_leva_da_api(config, pagina):
    """Busca a próxima leva, com o mesmo endereço que o site usa ao rolar a página."""
    params = {
        "_sort": f"{config['idioma']}_hits_last_7_days",
        "_page": pagina,
        # 1 = cifras (aba padrão da página, sem filtro "transcription")
        "version_transcription_type": 1,
    }
    headers = headers_padrao(config, 'application/json')
    headers['Origin'] = config['dominio']
    response = requests.get(API_EXPLORAR, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json().get('songs', []) or []

def extrair_musicas(config):
    lista_site = []   # a lista na ordem em que o site mostra (sem repetição)
    ids_vistos = set()
    repetidas = 0

    def emendar(leva):
        nonlocal repetidas
        for item in leva:
            song_id = item.get('id')
            chave_id = song_id if song_id is not None else f"{item.get('artist_slug')}/{item.get('slug')}"
            if chave_id in ids_vistos:
                repetidas += 1   # o site não mostra de novo
                continue
            ids_vistos.add(chave_id)
            lista_site.append(item)

    emendar(ler_primeira_leva_do_html(config))

    pagina = 2
    while len(lista_site) < TAMANHO_TOP and pagina <= MAX_PAGINAS:
        leva = buscar_leva_da_api(config, pagina)
        if not leva:
            break
        emendar(leva)
        pagina += 1

    print(f"   📄 {pagina - 1} levas carregadas; {repetidas} música(s) que o site esconde por já ter aparecido")

    musicas_atuais = {}
    for posicao, item in enumerate(lista_site[:TAMANHO_TOP], start=1):
        nome = (item.get('name') or "Desconhecido").strip()
        artista = (item.get('artist_name') or "Desconhecido").strip()

        # URL da música no domínio da própria região: /{artista}/{musica}/
        artista_slug = item.get('artist_slug') or ""
        musica_slug = item.get('slug') or ""
        link_absoluto = f"{config['dominio']}/{artista_slug}/{musica_slug}/" if (artista_slug and musica_slug) else ""

        # ⚡ ID ESTÁVEL: o "id" da página Explorar é o mesmo id de música que a
        # API antiga entregava, então o histórico já salvo continua casando dia
        # a dia, e o mesmo id aparece no BR e no Hispam ("Também aparece em").
        song_id = item.get('id')
        if song_id is not None:
            chave = str(song_id)
        elif link_absoluto:
            chave = urlparse(link_absoluto).path
        else:
            chave = f"{nome} - {artista}"

        musicas_atuais[chave] = {
            "posicao": posicao,
            "nome": nome,
            "artista": artista,
            "url": link_absoluto
        }

    if len(musicas_atuais) < TAMANHO_TOP:
        print(f"   ⚠️ Só {len(musicas_atuais)} músicas carregadas (o site acabou antes de {TAMANHO_TOP}).")

    return musicas_atuais

def buscar_dados_anteriores(regiao):
    data_hoje_iso = datetime.now().strftime("%Y-%m-%d")
    pasta_regiao = os.path.join(PASTA_DADOS, regiao)

    if os.path.exists(pasta_regiao):
        arquivos = sorted([
            f for f in os.listdir(pasta_regiao)
            if f.endswith('.json') and f != f"dados_{data_hoje_iso}.json"
        ])
        if arquivos:
            ultimo_arquivo = os.path.join(pasta_regiao, arquivos[-1])
            with open(ultimo_arquivo, 'r', encoding='utf-8') as f:
                return json.load(f)
    return {}

def atualizar_dados_dashboard(regiao):
    pasta_regiao = os.path.join(PASTA_DADOS, regiao)
    arquivos = sorted(glob.glob(os.path.join(pasta_regiao, "dados_*.json")))
    historico_global = {}
    todas_datas = []

    for arq in arquivos:
        nome_base = os.path.basename(arq)
        data_str = nome_base.replace("dados_", "").replace(".json", "")
        todas_datas.append(data_str)

        with open(arq, 'r', encoding='utf-8') as f:
            dados_dia = json.load(f)

        # A chave do dia já é o id estável da música (vindo direto da API),
        # então ela mesma é a "bucket" definitiva dentro de historico_global
        # — sem precisar da lógica de casamento por caminho/texto que o robô
        # antigo usava pra compensar o scraping de HTML sem id.
        for chave, info in dados_dia.items():
            if chave not in historico_global:
                historico_global[chave] = {}

            if info.get("url"):
                historico_global[chave]["url"] = info["url"]
            if info.get("nome"):
                historico_global[chave]["nome"] = info["nome"]
            if info.get("artista"):
                historico_global[chave]["artista"] = info["artista"]

            historico_global[chave][data_str] = info["posicao"]

    dados_finais = {
        "datas": todas_datas,
        "musicas": historico_global
    }

    with open(f"dados_dashboard_{regiao}.json", "w", encoding="utf-8") as f:
        json.dump(dados_finais, f, ensure_ascii=False, indent=4)

def processar_regiao(regiao, config):
    print(f"🎸 Coletando dados da região: {config['nome']} ({regiao})...")

    pasta_dados_regiao = os.path.join(PASTA_DADOS, regiao)
    pasta_relatorios_regiao = os.path.join(PASTA_RELATORIOS, regiao)
    os.makedirs(pasta_dados_regiao, exist_ok=True)
    os.makedirs(pasta_relatorios_regiao, exist_ok=True)

    atuais = extrair_musicas(config)
    if not atuais:
        print(f"⚠️ Alerta: Nenhuma música coletada para {config['nome']}. página Explorar/API mudou ou bloqueio.")
        return False

    anteriores = buscar_dados_anteriores(regiao)

    data_hoje_iso = datetime.now().strftime("%Y-%m-%d")
    data_hoje_br = datetime.now().strftime("%d/%m/%Y")

    novas_entradas = []
    subidas_absurdas = []
    grandes_saltos = []
    subidas_moderadas = []
    pequenas_subidas = []

    if not anteriores:
        conteudo_md = f"# 📊 Relatório Cifra Club - {config['nome']} - {data_hoje_br}\n\n"
        conteudo_md += f"ℹ️ **Base de dados de {config['nome']} estruturada com sucesso hoje!**\n"
        conteudo_md += "As movimentações e gráficos interativos começarão a rodar a partir do próximo ciclo de coleta.\n\n"
        conteudo_md += "### 📋 Prévia do Top 10 Atual:\n"
        for i, (chave, m) in enumerate(atuais.items(), start=1):
            if i > 10: break
            conteudo_md += f"{i}º. **{m['nome']}** — *{m['artista']}*\n"
    else:
        for chave, dados_atuais in atuais.items():
            pos_atual = dados_atuais['posicao']
            info_anterior = anteriores.get(chave)

            if info_anterior is None:
                novas_entradas.append(dados_atuais)
            else:
                pos_anterior = info_anterior['posicao']
                diferenca = pos_anterior - pos_atual

                dados_item = {
                    "dados": dados_atuais,
                    "pos_anterior": pos_anterior,
                    "pos_atual": pos_atual,
                    "posicoes_ganhas": diferenca
                }

                if diferenca > 400:
                    subidas_absurdas.append(dados_item)
                elif diferenca > 200:
                    grandes_saltos.append(dados_item)
                elif diferenca >= 100:
                    subidas_moderadas.append(dados_item)
                elif diferenca > MARGEM_OSCILACAO:
                    pequenas_subidas.append(dados_item)

        subidas_absurdas.sort(key=lambda x: x['posicoes_ganhas'], reverse=True)
        grandes_saltos.sort(key=lambda x: x['posicoes_ganhas'], reverse=True)
        subidas_moderadas.sort(key=lambda x: x['posicoes_ganhas'], reverse=True)
        pequenas_subidas.sort(key=lambda x: x['posicoes_ganhas'], reverse=True)

        conteudo_md = f"# 📊 Relatório Cifra Club - {config['nome']} - {data_hoje_br}\n\n"

        if subidas_absurdas:
            conteudo_md += "## 🚨 🚨 EXPLOSÃO NO TOP: SUBIDAS ABSURDAS (+400 posições) 🚨 🚨\n"
            for m in subidas_absurdas:
                conteudo_md += f"> ### 💥 **{m['dados']['nome']}** — *{m['dados']['artista']}*\n"
                conteudo_md += f"> 🛑 **Subida histórica!** Saltou de {m['pos_anterior']}º direto para **{m['pos_atual']}º** (🔼 **+{m['posicoes_ganhas']}** posições)\n\n"

        conteudo_md += "## 🔥 Grandes Saltos (+200 a 400 posições)\n"
        if grandes_saltos:
            for m in grandes_saltos:
                conteudo_md += f"- **{m['dados']['nome']}** ({m['dados']['artista']}): Subiu de {m['pos_anterior']}º para **{m['pos_atual']}º** (🔥 +{m['posicoes_ganhas']} posições)\n"
        else:
            conteudo_md += "- Nenhuma música com grande salto nesta faixa hoje.\n"

        conteudo_md += "\n## 📈 Subidas Significativas (100 a 200 posições)\n"
        if subidas_moderadas:
            for m in subidas_moderadas:
                conteudo_md += f"- **{m['dados']['nome']}** ({m['dados']['artista']}): Subiu de {m['pos_anterior']}º para **{m['pos_atual']}º** (📈 +{m['posicoes_ganhas']} posições)\n"
        else:
            conteudo_md += "- Nenhuma subida nesta faixa hoje.\n"

        conteudo_md += f"\n## 🌱 Pequenas Subidas (Abaixo de 100 posições)\n"
        conteudo_md += f"> Omitindo oscilações menores ou iguais a {MARGEM_OSCILACAO} posições.\n\n"
        if pequenas_subidas:
            for m in pequenas_subidas:
                conteudo_md += f"- **{m['dados']['nome']}** ({m['dados']['artista']}): {m['pos_anterior']}º → **{m['pos_atual']}º** (+{m['posicoes_ganhas']})\n"
        else:
            conteudo_md += "- Sem oscilações relevantes para cima hoje.\n"

        conteudo_md += "\n## 🚀 Novas Entradas no Top\n"
        if novas_entradas:
            for m in novas_entradas:
                conteudo_md += f"- **{m['nome']}** ({m['artista']}) - Apareceu direto na posição **{m['posicao']}º**\n"
        else:
            conteudo_md += "- Nenhuma música inédita detectada hoje.\n"

    # Salva os relatórios específicos da região
    with open(os.path.join(pasta_relatorios_regiao, f"relatorio_{data_hoje_iso}.md"), 'w', encoding='utf-8') as f:
        f.write(conteudo_md)

    # Relatório raiz específico da região (ex: relatorio_diario_hispam.md)
    with open(f"relatorio_diario_{regiao}.md", 'w', encoding='utf-8') as f:
        f.write(conteudo_md)

    # Salva o JSON na subpasta correspondente
    with open(os.path.join(pasta_dados_regiao, f"dados_{data_hoje_iso}.json"), 'w', encoding='utf-8') as f:
        json.dump(atuais, f, ensure_ascii=False, indent=4)

    return True

if __name__ == "__main__":
    try:
        # Define o alvo baseado no argumento do terminal (ex: "br", "hispam", ou "all")
        alvo = sys.argv[1].lower() if len(sys.argv) > 1 else "all"

        if alvo == "br":
            regioes_para_processar = ["br"]
        elif alvo == "hispam":
            regioes_para_processar = ["hispam"]
        else:
            regioes_para_processar = list(REGIOES.keys())

        print(f"🚀 Iniciando módulo de análise para o alvo: {alvo.upper()}")

        sucesso_geral = True
        for regiao in regioes_para_processar:
            config = REGIOES[regiao]
            try:
                if processar_regiao(regiao, config):
                    atualizar_dados_dashboard(regiao)
                    print(f"✅ Região {regiao.upper()} processada com sucesso.\n")
                else:
                    sucesso_geral = False
            except Exception as e:
                print(f"\n💥 Erro ao processar a região {regiao.upper()}:")
                traceback.print_exc()
                sucesso_geral = False

        if sucesso_geral:
            print(f"🚀 Módulo executado com sucesso total para as regiões ({alvo.upper()})!")
        else:
            print("⚠️ Execução concluída com falhas parciais em algumas regiões.")
            sys.exit(1)

    except Exception as e:
        print("\n💥 --- ERRO CRÍTICO INESPERADO NO SCRIPT --- 💥")
        traceback.print_exc()
        sys.exit(1)
