# Cahier des Charges — Agent Sentinel (v3)
## Veille Stratégique AI SaaS B2B

---

> **Projet** : Sentinel — Commando IA Veille Stratégique
> **Entreprise** : Welyne
> **Responsable** : Mohamed Ben Arfa (CEO)
> **Stagiaire** : Yassine
> **Durée** : 2 à 3 mois
> **Type** : MVP (Minimum Viable Product)
> **Usage** : Projet non commercial (stage / développement personnel — usage interne)
> **Date de rédaction** : Juin 2026
> **Version** : 3 (quotas free tier vérifiés · fallback LLM tiéré · keep-alive Supabase)

> ⚠️ **AVENANT v4 (août 2026) — à lire avec ce document.** L'équipe a spécifié deux
> **capacités spéciales** qui étendent le périmètre : un rapport de recherche approfondie
> **paramétré par l'utilisateur** (~3000 mots) et un **rapport comparatif concurrents**.
> Deux éléments listés « hors périmètre » au §10 sont de ce fait **entrés dans le
> périmètre**. Le corps du présent document décrit le MVP v3 tel que livré et **reste
> valide** ; les évolutions sont consignées au **§11 (Avenant v4)** en fin de document.

---

## 1. Contexte et objectifs

### 1.1 Contexte

Welyne est une entreprise IT spécialisée dans l'intelligence artificielle. Dans le cadre du programme **AI Commandos** — une flotte d'agents IA spécialisés destinés à automatiser des fonctions clés d'entreprise — Welyne souhaite développer l'agent **Sentinel**, son module de veille stratégique.

Ce projet est réalisé dans le cadre d'un stage de 2 à 3 mois. L'agent sera développé **from scratch**, sans données préexistantes ni intégrations disponibles, et sans recours à des outils ou APIs payants. Le projet a une finalité **non commerciale** (stage / développement personnel), ce qui rend l'usage des offres *free tier* des APIs conforme à leurs conditions d'utilisation.

> ⚠️ **Note ToS** : les niveaux gratuits utilisés (GNews, Gemini, etc.) sont autorisés pour un usage personnel / développement non commercial. Si Sentinel devait un jour évoluer vers une exploitation commerciale en production, les conditions d'utilisation de chaque API devront être réévaluées et, le cas échéant, migrées vers des offres adaptées.

### 1.2 Problème à résoudre

Le suivi du secteur de l'IA est aujourd'hui un travail **manuel et chronophage** : lire la presse spécialisée, surveiller les concurrents, détecter les tendances, compiler les informations dans un rapport. Ce travail est pourtant critique pour une entreprise évoluant dans ce secteur.

### 1.3 Objectif du projet

Construire un **MVP** de l'agent Sentinel : un workflow IA autonome, déployé et réellement utilisé par l'équipe Welyne à la fin du stage. Il surveille en continu le marché des outils AI SaaS B2B, en extrait les informations pertinentes, et délivre automatiquement des rapports de synthèse stratégique chaque semaine.

### 1.4 Nature du système — Workflow IA, pas agent autonome

Sentinel est techniquement un **AI-powered workflow** (pipeline automatisé), et non un agent IA au sens strict. Cette distinction est importante :

| Critère | Agent IA autonome | Sentinel (workflow IA) |
|---|---|---|
| **Comportement** | Décide lui-même quoi faire | Suit un enchaînement prédéfini |
| **Flux d'exécution** | Dynamique, peut boucler | Séquentiel, déterministe |
| **Adaptabilité** | S'adapte selon le contexte | Étapes fixes A → B → C → D |
| **Fiabilité MVP** | Plus difficile à maîtriser | Prévisible et robuste |
| **Cas d'usage** | Tâches complexes et ouvertes | Tâches répétitives et structurées ✅ |

> **Choix architectural** : pour des tâches répétitives et déterministes comme la veille hebdomadaire, un workflow est plus fiable, plus simple à déboguer et mieux adapté à un MVP de 2-3 mois. Le LLM intervient uniquement à l'étape d'analyse, dans un rôle ciblé et contrôlé.

### 1.5 Distinction POC vs MVP

Ce projet vise un **MVP**, non un simple POC.

| Critère | POC | MVP (ce projet) |
|---|---|---|
| **But** | Prouver la faisabilité | Livrer un produit utilisable |
| **Qualité du code** | Approximatif acceptable | Propre, structuré, maintenable |
| **Fiabilité** | Peut planter | Doit tourner sans surveillance |
| **Utilisateurs** | Le développeur | Le CEO et l'équipe Welyne |
| **Critère de succès** | "Ça marche une fois" | "Ça tourne chaque semaine tout seul" |

---

## 2. Périmètre de surveillance

### 2.1 Secteur cible

**AI SaaS B2B** — outils d'intelligence artificielle vendus aux entreprises, couvrant trois catégories :

| Catégorie | Acteurs suivis (exemples) |
|---|---|
| LLM Providers | OpenAI, Anthropic, Mistral AI, Google Gemini, Cohere, xAI |
| Plateformes d'automatisation | Make, Zapier AI, n8n |
| Plateformes d'agents | LangChain, CrewAI, Relevance AI, AutoGen |

> **Note MVP** : Commencer avec **2 catégories** et **5 à 8 acteurs** pour un périmètre maîtrisable, puis élargir si le temps le permet.

### 2.2 Signaux à détecter

- 🚀 Nouveaux produits ou fonctionnalités lancés
- 💰 Levées de fonds, acquisitions, partenariats
- 💲 Changements de pricing ou de modèle commercial
- 📈 Tendances et sujets émergents dans le secteur
- 🤝 Évolutions de positionnement et de messaging

### 2.3 Sources de données

La collecte privilégie **les APIs officielles gratuites et les flux RSS** avant tout scraping HTML, plus fiables et moins susceptibles d'être bloqués depuis un runner cloud (voir BF-01).

| Source | Type | Exemples / Détails |
|---|---|---|
| Médias tech spécialisés | RSS (gratuit) | TechCrunch, VentureBeat, Wired |
| Blogs officiels des acteurs | RSS (gratuit) | openai.com/blog, anthropic.com/news |
| Hacker News | API officielle gratuite | Algolia HN Search API (pas de scraping) |
| Product Hunt | API officielle gratuite | API GraphQL (pas de scraping) |
| Recherche actualités (découverte) | **Google News RSS Search** (gratuit, sans clé) | `news.google.com/rss/search?q=<mot-clé>` — pas de quota strict |
| Recherche actualités (découverte) | **GNews API (free tier)** | Recherche par mots-clés sectoriels — voir contraintes ci-dessous |

**Contraintes GNews free tier** (usage personnel / non commercial — conforme ToS) :
- 100 requêtes/jour · 1 req/seconde · **10 articles max par requête** · **snippets tronqués uniquement**.
- En conséquence, GNews est utilisé comme **couche de découverte** (trouver des articles pertinents par mot-clé), **pas** comme source de contenu.
- Le **texte complet** des articles retenus est récupéré depuis l'URL de l'article (RSS/fetch léger) uniquement pour ceux qui passent le filtre de pertinence.
- Google News RSS Search sert de source de découverte complémentaire pour contourner le quota et la limite de snippets de GNews.

---

## 3. Besoins fonctionnels

### BF-01 — Collecte automatisée des sources

L'agent doit **récupérer automatiquement** le contenu des sources définies, sans intervention manuelle, en privilégiant les APIs officielles et les flux RSS.

- Lecture des flux RSS des médias et blogs ciblés.
- **APIs officielles gratuites en priorité** pour Hacker News (Algolia) et Product Hunt (GraphQL) — pas de scraping HTML de ces sites.
- **Découverte par mots-clés** via Google News RSS Search + GNews API (ex : "OpenAI", "AI agents", "LLM").
- **Récupération du texte complet** depuis l'URL des articles retenus (GNews ne renvoyant que des snippets).
- Scraping HTML léger (`requests` + `BeautifulSoup`) réservé aux pages sans RSS ni API, dans le respect du `robots.txt` et avec un délai raisonnable entre requêtes.
- Horodatage (`collected_at`) de chaque article collecté pour le suivi temporel.

> ⚠️ **Contrainte cloud** : les sites protégés (Cloudflare, anti-bot) bloquent souvent les IP de datacenter (runners GitHub Actions). D'où la priorité donnée aux RSS et aux APIs officielles, plus robustes en environnement CI.

**Critère de validation** : L'agent collecte au moins 20 articles pertinents par cycle, sans intervention humaine, principalement via RSS et APIs officielles.

---

### BF-02 — Filtrage et déduplication

Les données brutes collectées doivent être nettoyées avant analyse.

- **Déduplication exacte** via URL unique en base de données (contrainte `UNIQUE`).
- **Déduplication inter-sources (best effort)** : une même actualité reprise par plusieurs médias a des URL différentes. Un rapprochement par **similarité de titres** (normalisation + distance/fuzzy matching) regroupe ces doublons pour ne pas les compter plusieurs fois dans l'analyse.
- Filtrage par mots-clés de pertinence (liste paramétrable dans un fichier de config).
- Exclusion des articles hors périmètre sectoriel.
- Marquage des articles déjà traités lors des cycles précédents (`processed = TRUE`).

**Critère de validation** : Moins de 5 % de **doublons exacts** (même URL) dans les articles soumis à l'analyse IA ; réduction visible des doublons inter-sources grâce à la similarité de titres.

---

### BF-03 — Analyse IA

Le LLM traite les articles filtrés pour en extraire de l'intelligence.

**Modèle utilisé** : **Google Gemini API (free tier — famille Flash)** en principal, **Groq (`llama-3.3-70b-versatile`) en solution de repli tiérée**.

- **Modèles Gemini free tier (mi-2026)** : famille **Flash** uniquement — `Gemini 2.5 Flash`, `Gemini 3 Flash`, ou `Gemini 3.1 Flash-Lite` (nom paramétré en config). ⚠️ Le free tier de la famille **Pro** a été supprimé en avril 2026 : s'en tenir strictement à la famille Flash.
- **Quotas Gemini (à confirmer au démarrage)** : ~10–15 requêtes/minute (RPM) · 1 500 requêtes/jour (RPD) · 250 000 à 1 000 000 tokens/minute (TPM) selon la version. Le RPD est largement suffisant pour une exécution hebdomadaire.
- **Justification** : le grand contexte de Gemini Flash (~1M tokens) permet d'ingérer en une passe les articles de la semaine **et** l'historique des tendances des 4 semaines précédentes ; free tier généreux ; bonne qualité de résumé en anglais et en français.
- **Batching (respect du RPM)** : ne pas faire un appel par article (20+ articles → risque de throttling à ~10-15 RPM). Regrouper **plusieurs articles par requête** pour les résumés/classification — plus économe en tokens et compatible avec le plafond RPM.
- **Repli Groq — tiéré, pas identique** : `llama-3.3-70b-versatile` est disponible en free tier mais limité à **12 000 TPM / 30 RPM / 1 000 RPD** (limites au niveau de l'organisation — impossible de contourner avec plusieurs clés). Ce plafond de 12 000 TPM est **trop serré** pour l'analyse à contexte historique. En conséquence :
  - L'**analyse de tendances à contexte historique** (articles + 4 semaines) reste **exclusivement sur Gemini**.
  - Groq n'assure en repli que les **tâches légères en tokens** (résumés, classification) par petits lots.
  - Si Gemini est indisponible, le pipeline **dégrade gracieusement** : résumés via Groq + un **digest compressé** des tendances (jamais l'historique brut), plutôt que de saturer la fenêtre de 12 000 TPM.
- **Note confidentialité** : le free tier Gemini peut utiliser les entrées pour améliorer le service. Acceptable ici car les sources sont **publiques** ; à réévaluer en cas de passage en production.
- **Note stabilité** : les versions de modèles évoluent ; le nom du modèle est paramétré en config, jamais codé en dur.

Tâches réalisées par le LLM :

- **Résumé** : produire un résumé de 3 à 5 lignes par article pertinent.
- **Classification par tags canoniques** : rattacher chaque article à 0..n **sujets d'une liste canonique prédéfinie** (fichier de config, ~15-25 tags). Cela évite que le LLM invente des libellés de sujets variables ("multi-modal agents" vs "multimodal AI agents") et fiabilise le comptage des tendances (voir BF-04).
- **Comparaison concurrentielle** : identifier qui fait quoi parmi les acteurs suivis.
- **Détection de tendances** : repérer les sujets récurrents sur la semaine courante ET sur les semaines précédentes (grâce à la mémoire historique).
- **Extraction d'opportunités** : signaler ce qui pourrait être pertinent pour Welyne.

> **Garde-fou anti-hallucination** : chaque opportunité et chaque affirmation du rapport doit être **rattachée à un ou plusieurs articles sources cités**. Le LLM ne doit rien affirmer qui ne soit ancré dans les articles fournis en contexte.

> Le LLM reçoit en contexte les articles de la semaine **et** un résumé des tendances des 4 semaines précédentes, ce qui lui permet de distinguer une nouvelle tendance d'une tendance en accélération.

**Critère de validation** : Les résumés générés sont cohérents, sourcés et exploitables sans relecture de l'article source.

---

### BF-04 — Mémoire historique

Pour améliorer la qualité de la détection de tendances, le système conserve un **historique structuré** dans une base de données **Supabase (PostgreSQL managé, free tier)**. Chaque cycle de collecte alimente cet historique plutôt que de repartir de zéro.

> 🔑 **Décision d'architecture (correction v2)** : la mémoire **ne peut pas** vivre dans un fichier SQLite local, car les runners GitHub Actions sont **éphémères** (système de fichiers réinitialisé à chaque exécution). L'historique serait perdu à chaque cycle. **Supabase** fournit une base PostgreSQL **persistante et hébergée dans le cloud**, accessible depuis le runner via une simple chaîne de connexion / clé API stockée en secret. C'est ce qui rend la mémoire historique réellement fonctionnelle.

> ⚠️ **Limites free tier Supabase (v3)** :
> - **Taille base 500 Mo** (+ 1 Go de stockage fichiers) : très suffisant pour du texte + métadonnées sur un MVP de 2-3 mois.
> - **Mise en pause après 7 jours d'inactivité** : *critique* pour un pipeline hebdomadaire. La base risque d'être en pause **exactement** au moment où le cron s'exécute ; le réveil prend ~30 s et provoquerait un timeout.
> - **Solution — keep-alive** : un **workflow GitHub Actions secondaire et léger** exécute une requête `SELECT 1` **tous les 3 à 4 jours** pour réinitialiser le compteur d'inactivité (gratuit, quelques secondes).
> - **Ceinture + bretelles** : la connexion du pipeline principal intègre en plus un **retry avec backoff**, pour qu'un éventuel réveil à froid coûte au pire une nouvelle tentative plutôt qu'une exécution échouée.

Schéma de base de données (PostgreSQL) — trois tables :

```sql
-- Tous les articles collectés (mémoire permanente)
create table articles (
    id            bigint generated always as identity primary key,
    url           text unique not null,   -- clé de déduplication exacte
    title         text,
    source        text,
    actor         text,                   -- ex: "OpenAI", "Mistral"
    topics        text[],                 -- tags canoniques attribués par le LLM
    published_at  timestamptz,
    collected_at  timestamptz default now(),
    summary       text,                   -- résumé généré par le LLM
    processed     boolean default false
);

-- Fréquence des sujets semaine par semaine
create table trends (
    id            bigint generated always as identity primary key,
    topic         text,                   -- issu de la liste canonique (BF-03)
    week          text,                   -- ex: "2026-W24"
    article_count integer,                -- nb d'articles sur ce sujet cette semaine
    actors        jsonb                   -- acteurs concernés
);

-- Rapports générés (archivage)
create table reports (
    id            bigint generated always as identity primary key,
    week          text,
    generated_at  timestamptz default now(),
    content_html  text
);
```

**Bootstrap de l'historique (Sprint 1)** : la détection de tendances en accélération n'a de sens qu'avec plusieurs semaines de données. Pour disposer d'un historique dès le premier rapport, un **backfill initial** exécute la collecte sur les archives RSS / actualités des semaines passées et pré-remplit la table `trends`. Sans cela, la fonctionnalité phare ne serait démontrable qu'en toute fin de stage.

**Bénéfice** : le rapport peut indiquer "ce sujet est mentionné pour la 3e semaine consécutive, avec une forte accélération" — bien plus actionnable qu'un simple résumé hebdomadaire isolé.

**Critère de validation** : Le rapport distingue les nouvelles tendances des tendances en accélération, avec référence aux semaines précédentes, dès les premiers cycles grâce au backfill.

---

### BF-05 — Génération du rapport

L'agent produit un rapport structuré, lisible et exploitable, en format HTML (compatible email).

Contenu du rapport hebdomadaire :

1. **Résumé exécutif** — les 3 à 5 faits marquants de la semaine (1 page max)
2. **Veille concurrentielle** — ce que chaque acteur surveillé a fait cette semaine
3. **Tendances détectées** — sujets émergents + tendances en accélération (avec contexte historique)
4. **Opportunités pour Welyne** — recommandations issues de l'analyse, **chacune reliée à ses articles sources**
5. **Sources consultées** — liste des articles analysés avec liens

**Critère de validation** : Le rapport est généré sans intervention humaine, directement lisible par le CEO, et chaque insight est traçable jusqu'à sa source.

---

### BF-06 — Diffusion automatique

Le rapport est envoyé automatiquement aux destinataires définis, sans action manuelle.

- Envoi par email via **Gmail SMTP** (gratuit). ⚠️ Nécessite l'activation de la double authentification et un **mot de passe d'application** dédié (stocké en secret, jamais en clair).
- Envoi optionnel vers un canal Slack via **webhook Slack** (gratuit).
- Liste de destinataires paramétrable dans le fichier de configuration.
- Objet de l'email automatique avec la date du rapport.

**Critère de validation** : Le CEO reçoit le rapport par email chaque lundi matin sans aucune action manuelle.

---

### BF-07 — Planification et déploiement

L'ensemble du pipeline s'exécute de façon planifiée et autonome dans le cloud.

- Déclenchement automatique via **GitHub Actions** (cron hebdomadaire, gratuit).
- ⚠️ **Fuseau horaire** : le cron GitHub Actions s'exécute en **UTC**. L'horaire doit être décalé pour que « lundi matin » corresponde bien à l'heure française (CET/CEST).
- **Gestion des secrets** : clés API (Gemini, Groq, GNews), identifiants Gmail, connexion Supabase et webhook Slack sont stockés dans les **GitHub Secrets** — **jamais** commités dans le dépôt. Seule la configuration non sensible (listes d'acteurs, mots-clés, tags canoniques, destinataires) vit dans `config.yaml`.
- Journalisation des exécutions (logs) pour le suivi et le débogage.
- Gestion des erreurs : si une source est indisponible, le pipeline continue sur les autres (dégradation gracieuse).

**Critère de validation** : L'agent s'exécute de façon autonome pendant 2 semaines consécutives sans intervention.

---

## 4. Architecture technique

Le pipeline de Sentinel est composé de 4 étapes séquentielles. La base de données **Supabase (PostgreSQL cloud)** joue un double rôle : stockage courant **et** mémoire historique inter-cycles, **persistante** entre les exécutions du runner.

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐     ┌─────────────┐
│   COLLECTE  │────▶│  TRAITEMENT  │────▶│  GÉNÉRATION  │────▶│  DIFFUSION  │
│             │     │              │     │              │     │             │
│ RSS Fetcher │     │ Filtre/Dédup │     │ Gemini (LLM) │     │ Gmail SMTP  │
│ HN / PH API │     │ (URL + fuzzy)│◀───▶│ Groq (repli) │     │ Slack Hook  │
│ Google News │     │  + Tagging   │     │ Jinja2 HTML  │     │             │
│ GNews (déc.)│     │              │     │              │     │             │
└─────────────┘     └──────────────┘     └──────────────┘     └─────────────┘
                          ▲ ▼
                    ┌──────────────┐
                    │   MÉMOIRE     │
                    │  HISTORIQUE   │  ← articles, trends, reports (PERSISTANT)
                    │  (Supabase /  │
                    │  PostgreSQL)  │
                    └──────────────┘
       ▲
       │
  ┌───────────────┐
  │ GitHub Actions │  ← déclenche tout le pipeline (cron UTC), tourne dans le cloud
  │  (cron free)   │     runner éphémère → l'état vit dans Supabase, pas sur disque
  └───────────────┘

  ┌────────────────────┐
  │ Keep-alive workflow │  ← SELECT 1 tous les 3-4 jours → empêche la pause Supabase (7 j)
  │  (GitHub Actions)   │
  └────────────────────┘
```

---

## 5. Stack technique — 100 % gratuit

| Brique | Outil | Justification |
|---|---|---|
| **Langage** | Python 3.11+ | Standard IA, écosystème large |
| **LLM (principal)** | Google Gemini API (famille Flash, free tier) | Gratuit · grand contexte (~1M tokens) pour articles + historique · ~1500 RPD · bon en FR/EN |
| **LLM (repli tiéré)** | Groq API (`llama-3.3-70b-versatile`) | Gratuit · rapide · **12k TPM** → réservé aux tâches légères (résumés/classification) |
| **Collecte RSS** | `feedparser` | Python natif · 100 % gratuit |
| **Découverte actualités** | Google News RSS Search + GNews (free tier) | Sans clé (Google News) · GNews en complément par mot-clé |
| **APIs sources officielles** | Algolia HN Search · Product Hunt GraphQL | Gratuites · plus fiables que le scraping |
| **Scraping (fallback)** | `requests` + `BeautifulSoup` | Léger · gratuit · réservé aux pages sans RSS/API |
| **Stockage & mémoire** | **Supabase (PostgreSQL, free tier)** | Cloud · **persistant** · 500 Mo (suffisant) · pause après 7 j → keep-alive requis |
| **Keep-alive DB** | 2ᵉ workflow GitHub Actions (`SELECT 1`) | Gratuit · tous les 3-4 j · évite la mise en pause Supabase |
| **Templates rapport** | Jinja2 | Gratuit · rendu HTML propre |
| **Scheduling & déploiement** | GitHub Actions (cron) | Gratuit · cloud · logs intégrés |
| **Email** | Gmail SMTP (`smtplib`) | Gratuit · natif Python · mot de passe d'application requis |
| **Slack** | Incoming Webhooks Slack | Gratuit · une URL à configurer |
| **Logs** | `logging` (natif Python) | Gratuit · suffisant pour un MVP |

> ✅ **Aucun outil payant, aucune carte bancaire requise.** Tous les outils ci-dessus ont une version gratuite suffisante pour le volume d'un MVP sectoriel non commercial.
> ⚠️ **Quotas free tier vérifiés (mi-2026, à reconfirmer au démarrage)** : Gemini Flash ~10-15 RPM / 1 500 RPD / 250k-1M TPM · Groq 70B 30 RPM / 12 000 TPM / 1 000 RPD (limites au niveau organisation) · Supabase 500 Mo, pause à 7 j · GNews 100 req/j, 10 articles/req, snippets.

---

## 6. Contraintes du projet

| Contrainte | Description |
|---|---|
| **Usage non commercial** | Projet de stage / développement personnel — conforme aux free tiers utilisés |
| **Zéro outil payant** | Stack 100 % gratuit — APIs free tier, bibliothèques open source |
| **Zéro intégration existante** | Tout est construit from scratch |
| **Persistance obligatoire** | L'état (mémoire historique) doit survivre entre exécutions → Supabase, pas de stockage local |
| **Anti-pause Supabase** | Keep-alive tous les 3-4 j (free tier pausé à 7 j d'inactivité) + retry/backoff à la connexion |
| **Budget tokens LLM** | Analyse historique sur Gemini uniquement ; Groq (12k TPM) réservé aux tâches légères ; batching des appels |
| **Secrets protégés** | Toutes les clés/identifiants dans GitHub Secrets, jamais dans le dépôt |
| **Durée** | MVP livré et déployé en 2 à 3 mois |
| **Maintenabilité** | Code documenté et repris par l'équipe après le stage |
| **Sources publiques** | Uniquement des sources accessibles sans abonnement |
| **Déploiement cloud** | L'agent tourne sans machine locale allumée (GitHub Actions) |

---

## 7. Livrables attendus

| Livrable | Description | Échéance |
|---|---|---|
| **L1 — Agent déployé** | MVP fonctionnel, en production sur GitHub Actions | Fin de stage |
| **L2 — Code source** | Code commenté, structuré en modules, déposé sur GitHub | Fin de stage |
| **L3 — Documentation** | Guide d'installation, configuration et utilisation | Fin de stage |
| **L4 — Exemple de rapport** | Un rapport réel généré par l'agent | Avant-dernière semaine |
| **L5 — Présentation de démo** | Démonstration live du pipeline end-to-end | Dernière semaine |

---

## 8. Planning

Le stage est découpé en **6 sprints de 2 semaines** (sur 12 semaines). L'itération sur les prompts et la qualité d'analyse est volontairement étalée sur S3–S4 (partie la plus délicate).

| Sprint | Semaines | Objectif | Tâches principales |
|---|---|---|---|
| **S1** | 1 – 2 | Fondations | Setup Python + GitHub · collecte RSS · APIs HN/Product Hunt · Google News RSS + GNews · **provisioning Supabase + schéma + keep-alive** · **backfill historique initial** |
| **S2** | 3 – 4 | Traitement | Filtrage mots-clés · déduplication (URL + similarité titres) · liste de tags canoniques · alimentation `trends` · logs |
| **S3** | 5 – 6 | Analyse IA | Intégration Gemini (+ fallback Groq tiéré) · **batching des appels** · prompts résumé & classification par tags · comparaison concurrentielle · contexte historique · **début itération qualité** |
| **S4** | 7 – 8 | Rapport & analyse (suite) | Templates Jinja2 · génération HTML · détection tendances (nouvelles vs accélération) · opportunités sourcées · **poursuite itération prompts** |
| **S5** | 9 – 10 | Diffusion & Deploy | Gmail SMTP (mot de passe d'application) · Slack webhook · GitHub Actions cron (fuseau UTC) · secrets · config |
| **S6** | 11 – 12 | Stabilisation | Tests end-to-end · gestion d'erreurs · documentation · démo finale |

---

## 9. Critères de succès du MVP

Le MVP est considéré comme livré si :

- [ ] L'agent s'exécute **de façon 100 % autonome** via GitHub Actions
- [ ] La **mémoire historique persiste** entre les exécutions (Supabase)
- [ ] Un **keep-alive** empêche la mise en pause de la base sur toute la durée d'exploitation
- [ ] Un rapport est **généré et envoyé chaque semaine** sans action manuelle
- [ ] Le rapport contient : résumé exécutif, veille concurrentielle, tendances (avec contexte historique), opportunités **sourcées**
- [ ] Le pipeline tourne **sans interruption pendant 2 semaines consécutives**
- [ ] Le code est **déposé sur GitHub**, documenté, et repris possible par un tiers
- [ ] Les secrets sont **hors du dépôt** (GitHub Secrets)
- [ ] **Aucun outil payant** n'est utilisé dans la solution finale
- [ ] Le CEO valide la **pertinence des insights** produits par l'agent

---

## 10. Hors périmètre (MVP)

Les éléments suivants sont **exclus** et pourront être envisagés dans une version ultérieure :

- Interface utilisateur web ou dashboard de visualisation
- ~~Personnalisation dynamique des rapports par l'utilisateur~~ → **entré dans le périmètre (avenant v4, §11)**
- Surveillance de Twitter/X (API payante)
- Analyse des réseaux sociaux (restrictions légales et techniques)
- Alertes en temps réel (le MVP est hebdomadaire)
- ~~Multi-langues (le MVP cible les sources en anglais)~~ → **partiellement entré dans le périmètre (avenant v4, §11)** : la langue du **rapport** est désormais paramétrable (FR/EN) ; les **sources** restent majoritairement anglophones
- LLMs ou APIs payants
- Exploitation **commerciale** en production (nécessiterait une réévaluation des ToS et des offres)

---

## 11. Avenant v4 — Capacités spéciales paramétrées (août 2026)

> Cet avenant complète le cahier des charges v3 sans le remplacer. Le MVP décrit aux
> §1–10 est **livré et en production** ; ce qui suit décrit l'extension demandée par
> l'équipe et les décisions techniques associées.

### 11.1 Nouvelles capacités demandées

**Capacité 1 — Rapport de veille stratégique approfondi, périodique et paramétré.**
L'utilisateur renseigne six variables via un formulaire de requête :

| Variable | Description | Exemple |
|---|---|---|
| {A} | Thème du rapport | « L'IA dans le secteur de la santé » |
| {B} | Langue du rapport (défaut : celle de l'on-boarding) | français |
| {C} | Zone géographique prioritaire | Monde / Europe / France / Amérique du Nord / Asie |
| {D} | Horizon temporel | 12 derniers mois + projection 3–5 ans |
| {E} | Focus sectoriel | santé, retail, éducation, industrie… |
| {F} | Objectif prioritaire du dirigeant | croissance, innovation, réduction des risques, M&A… |

Livrable : **~3000 mots (± 250)**, ton dirigeant, structure obligatoire en trois parties
(synthèse opérationnelle · analyse détaillée incl. signaux faibles et scénarios ·
opportunités, risques & implications), **sources nommées et crédibles** (McKinsey, BCG,
Bain, Deloitte, PwC, Gartner, Forrester, IDC, données publiques, rapports
institutionnels), en HTML propre directement collable dans un email.

**Capacité 2 — Rapport comparatif concurrents.** 5 à 10 concurrents classés par niveau
de menace, dossier par concurrent (offre, positionnement, cible, forces, faiblesses),
risques pour l'entreprise, opportunités exploitables, puis synthèse stratégique (top 3 +
plan d'action). S'appuie sur un **profil d'entreprise** stocké.

### 11.2 Impact sur le périmètre v3

| Élément v3 | Évolution |
|---|---|
| Thème unique « AI SaaS B2B » codé en dur | Devient un **paramètre** ({A}) ; le thème d'origine reste une requête parmi d'autres |
| Rapport en anglais | Langue **paramétrable** ({B}) ; libellés FR/EN |
| Cadence hebdomadaire fixe | Cadence **par requête** (hebdo / mensuel / trimestriel), déclarée dans le fichier de requête et exécutée par un répartiteur quotidien |
| Rapport ≈ 550 mots de narration | Nouveau format ≈ **3000 mots** (le rapport hebdomadaire v3 est conservé tel quel) |
| Sources = articles collectés uniquement | Ajout d'une **recherche web ancrée** pour atteindre les publications des cabinets nommés |

### 11.3 Décisions techniques (v4)

- **Recherche web via le grounding Google Search de l'API Gemini** — vérifié disponible
  en *free tier* (août 2026) : une requête a retourné 31 sources dont `mckinsey.com`,
  `bcg.com`, `deloitte.com`, `gartner.com`, `forrester.com`, `idc.com`, `who.int`,
  `oecd.org`. **Aucune nouvelle dépendance ni service payant.** Contrainte constatée :
  le grounding est **incompatible avec le mode JSON**, d'où une architecture en deux
  temps (acquisition ancrée en prose → rédaction structurée non ancrée).
- **Garde-fou anti-hallucination renforcé, non assoupli.** La règle v3 (« toute
  affirmation cite un article collecté, sinon elle est supprimée ») rendait impossibles
  les citations de cabinets et les projections à 3–5 ans. Elle est remplacée par
  **trois niveaux contrôlés par le code** :
  - **A** — cite un article collecté par Sentinel ;
  - **B** — cite une source web **réellement récupérée** par la recherche ;
  - **C** — projection / scénario / hypothèse argumentée : autorisée **uniquement** si
    rattachée à une preuve A ou B **et** affichée avec une **mention explicite
    d'hypothèse**.
  Le modèle **n'écrit jamais d'URL** : il ne manipule que des identifiants de preuve, ce
  qui rend une citation inventée structurellement impossible. Tout élément rejeté est
  **compté** (et non silencieusement reformulé) et le décompte figure dans le rapport.
- **Requêtes déclarées en fichiers YAML** (`requests/<slug>.yaml`), versionnés dans le
  dépôt — pas d'interface web (celle-ci reste hors périmètre, §10). Les réponses
  d'**on-boarding** communes à toute l'organisation (langue {B} par défaut,
  destinataires, profil d'entreprise) sont centralisées une seule fois dans
  `profiles/onboarding.yaml` ; une requête n'a besoin de déclarer que ce qui lui est
  propre.
- **Réception périodique sans planificateur d'état.** Chaque requête porte sa cadence ;
  un workflow **quotidien** produit ce qui est dû. L'échéance n'est pas stockée sous
  forme de « prochaine exécution » (valeur dérivée qui dérive dès qu'une exécution est
  manquée) mais **déduite du calendrier** : une requête est due si l'on se trouve dans
  une période de sa cadence et qu'aucune exécution n'existe encore pour cette période,
  d'après le registre `runs`. Conséquences : un jour manqué se rattrape, une double
  exécution ne produit rien la seconde fois, et un échec est **réessayé le lendemain**
  plutôt que perdu jusqu'à la période suivante.
- **Mémoire de tendances par requête.** La table `trends` était clé-unique
  `(topic, week)` **globalement** — correct tant que Sentinel surveillait un seul thème,
  faux dès qu'il en surveille plusieurs : deux requêtes comptant le même tag la même
  semaine s'écrasaient mutuellement. La clé est désormais `(request_slug, week, topic)` et
  le *scope* est porté par le **dépôt** (`TrendRepository(db, scope=...)`), qui filtre
  chaque lecture *et* chaque écriture — aucune étape du pipeline n'a eu à changer. Une
  requête reçoit sa propre histoire **par défaut** : le partage est explicite, jamais un
  oubli. Le scope `__default__` désigne la veille historique et détient toutes les lignes
  antérieures. Reste refusé le seul cas qu'aucune clé ne peut départager : une **taxonomie
  propre** écrite dans un **scope partagé**.
- **Signaux faibles = une lecture, pas une seconde table.** `detect_weak_signals()` est une
  fonction pure sur les mêmes comptages (« un sujet qui revient sans jamais grossir »),
  ce qui évite un second chemin d'écriture susceptible de diverger.
- **Non-régression** : le rapport hebdomadaire v3 est protégé par un test de comparaison
  **octet par octet** ; toutes les évolutions v4 sont additives et désactivées par défaut
  en l'absence de profil de requête. La veille hebdomadaire de production n'est
  **volontairement pas** confiée au répartiteur (`scheduled: false`) : `weekly.yml` la
  pilote déjà, et deux pilotes signifieraient deux emails le lundi.

### 11.4 Points restant à confirmer avec l'équipe

- « ~3000 mots » : **inclut ou exclut** la liste des sources en annexe ?
- ~~Existe-t-il un **formulaire / on-boarding** existant dont l'agent doit lire les
  paramètres, ou les fichiers YAML font-ils foi ?~~ → en attendant une réponse, les
  réponses d'on-boarding sont modélisées dans `profiles/onboarding.yaml` ; brancher un
  formulaire existant reviendrait à remplacer le chargement de ce fichier, sans toucher
  au reste.
- Destinataires et thèmes prioritaires pour les premières requêtes en production —
  aujourd'hui : `ai_healthcare_fr` (mensuel) et `competitors_quarterly` (trimestriel).

---

*Document rédigé dans le cadre du stage Welyne — Agent Sentinel (Veille Stratégique AI SaaS B2B).*
*Corps : v3 (révision après revue technique). Avenant §11 : v4, août 2026.*
