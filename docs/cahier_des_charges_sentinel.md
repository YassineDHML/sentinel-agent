# Cahier des Charges — Agent Sentinel
## Veille Stratégique AI SaaS B2B

---

> **Projet** : Sentinel — Commando IA Veille Stratégique  
> **Entreprise** : Welyne  
> **Responsable** : Mohamed Ben Arfa (CEO)  
> **Stagiaire** : Yassine  
> **Durée** : 2 à 3 mois  
> **Type** : MVP (Minimum Viable Product)  
> **Date de rédaction** : Juin 2025

---

## 1. Contexte et objectifs

### 1.1 Contexte

Welyne est une entreprise IT spécialisée dans l'intelligence artificielle. Dans le cadre du programme **AI Commandos** — une flotte d'agents IA spécialisés destinés à automatiser des fonctions clés d'entreprise — Welyne souhaite développer l'agent **Sentinel**, son module de veille stratégique.

Ce projet est réalisé dans le cadre d'un stage de 2 à 3 mois. L'agent sera développé **from scratch**, sans données préexistantes ni intégrations disponibles, et sans recours à des outils ou APIs payants.

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

Ce projet vise un **MVP**, non un simple POC. Cette distinction est importante :

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

| Source | Type | Exemples |
|---|---|---|
| Médias tech spécialisés | RSS (gratuit) | TechCrunch, VentureBeat, Wired |
| Blogs officiels des acteurs | RSS / scraping (gratuit) | openai.com/blog, anthropic.com/news |
| Agrégateurs communautaires | Scraping (gratuit) | Product Hunt, Hacker News |
| Moteur de recherche actualités | GNews API (free tier) | Recherche par mots-clés sectoriels |

---

## 3. Besoins fonctionnels

### BF-01 — Collecte automatisée des sources

L'agent doit être capable de **récupérer automatiquement** le contenu des sources définies, sans intervention manuelle.

- Lecture des flux RSS des médias et blogs ciblés
- Scraping léger des pages sans flux RSS disponible
- Interrogation de GNews API par mots-clés (ex : "OpenAI", "AI agents", "LLM")
- Horodatage de chaque article collecté pour le suivi temporel

**Critère de validation** : L'agent collecte au moins 20 articles pertinents par cycle, sans intervention humaine.

---

### BF-02 — Filtrage et déduplication

Les données brutes collectées doivent être nettoyées avant analyse.

- Suppression des articles en double via URL unique en base de données
- Filtrage par mots-clés de pertinence (liste paramétrable dans un fichier de config)
- Exclusion des articles hors périmètre sectoriel
- Marquage des articles déjà traités lors des cycles précédents (`processed = TRUE`)

**Critère de validation** : Moins de 5 % de doublons dans les articles soumis à l'analyse IA.

---

### BF-03 — Analyse IA

Le LLM traite les articles filtrés pour en extraire de l'intelligence. Modèle utilisé : **Llama 3.1 ou Mixtral via Groq API (gratuit)**.

- **Résumé** : produire un résumé de 3 à 5 lignes par article pertinent
- **Comparaison concurrentielle** : identifier qui fait quoi parmi les acteurs suivis
- **Détection de tendances** : repérer les sujets récurrents sur la semaine courante ET sur les semaines précédentes (grâce à la mémoire historique)
- **Extraction d'opportunités** : signaler ce qui pourrait être pertinent pour Welyne

> Le LLM reçoit en contexte les articles de la semaine **et** un résumé des tendances des 4 semaines précédentes, ce qui lui permet de distinguer une nouvelle tendance d'une tendance en accélération.

**Critère de validation** : Les résumés générés sont cohérents et exploitables sans relecture de l'article source.

---

### BF-04 — Mémoire historique

Pour améliorer la qualité de la détection de tendances, le système conserve un **historique structuré** dans la base de données SQLite. Chaque cycle de collecte alimente cet historique plutôt que de repartir de zéro.

Le schéma de base de données comprend trois tables :

```sql
-- Tous les articles collectés (mémoire permanente)
CREATE TABLE articles (
    id           INTEGER PRIMARY KEY,
    url          TEXT UNIQUE,       -- clé de déduplication
    title        TEXT,
    source       TEXT,
    actor        TEXT,              -- ex: "OpenAI", "Mistral"
    published_at DATETIME,
    summary      TEXT,              -- résumé généré par le LLM
    processed    BOOLEAN DEFAULT 0
);

-- Fréquence des sujets semaine par semaine
CREATE TABLE trends (
    id            INTEGER PRIMARY KEY,
    topic         TEXT,             -- ex: "multi-modal agents"
    week          TEXT,             -- ex: "2025-W24"
    article_count INTEGER,          -- nombre d'articles sur ce sujet cette semaine
    actors        TEXT              -- acteurs concernés (JSON)
);

-- Rapports générés (archivage)
CREATE TABLE reports (
    id            INTEGER PRIMARY KEY,
    week          TEXT,
    generated_at  DATETIME,
    content_html  TEXT
);
```

**Bénéfice** : le rapport peut indiquer "ce sujet est mentionné pour la 3e semaine consécutive, avec une forte accélération" — une information bien plus actionnable qu'un simple résumé hebdomadaire isolé.

**Critère de validation** : Le rapport distingue les nouvelles tendances des tendances en accélération, avec référence aux semaines précédentes.

---

### BF-05 — Génération du rapport

L'agent produit un rapport structuré, lisible et exploitable, en format HTML (compatible email).

Contenu du rapport hebdomadaire :

1. **Résumé exécutif** — les 3 à 5 faits marquants de la semaine (1 page max)
2. **Veille concurrentielle** — ce que chaque acteur surveillé a fait cette semaine
3. **Tendances détectées** — sujets émergents + tendances en accélération (avec contexte historique)
4. **Opportunités pour Welyne** — recommandations issues de l'analyse
5. **Sources consultées** — liste des articles analysés avec liens

**Critère de validation** : Le rapport est généré sans intervention humaine et directement lisible par le CEO.

---

### BF-06 — Diffusion automatique

Le rapport est envoyé automatiquement aux destinataires définis, sans action manuelle.

- Envoi par email via **Gmail SMTP** (gratuit)
- Envoi optionnel vers un canal Slack via **webhook Slack** (gratuit)
- Liste de destinataires paramétrable dans le fichier de configuration
- Objet de l'email automatique avec la date du rapport

**Critère de validation** : Le CEO reçoit le rapport par email chaque lundi matin sans aucune action manuelle.

---

### BF-07 — Planification et déploiement

L'ensemble du pipeline s'exécute de façon planifiée et autonome dans le cloud.

- Déclenchement automatique via **GitHub Actions** (cron hebdomadaire, gratuit)
- Journalisation des exécutions (logs) pour le suivi et le débogage
- Gestion des erreurs : si une source est indisponible, le pipeline continue sur les autres
- Configuration centralisée dans un fichier `.env` ou `config.yaml`

**Critère de validation** : L'agent s'exécute de façon autonome pendant 2 semaines consécutives sans intervention.

---

## 4. Architecture technique

Le pipeline de Sentinel est composé de 4 étapes séquentielles. La base de données SQLite joue un double rôle : stockage courant **et** mémoire historique inter-cycles.

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐     ┌─────────────┐
│   COLLECTE  │────▶│  TRAITEMENT  │────▶│  GÉNÉRATION  │────▶│  DIFFUSION  │
│             │     │              │     │              │     │             │
│ RSS Fetcher │     │ Stockage brut│     │  Groq + LLM  │     │ Gmail SMTP  │
│ Web Scraper │     │ Filtre/Dédup │◀───▶│ Jinja2 HTML  │     │ Slack Hook  │
│ GNews API   │     │   SQLite     │     │              │     │             │
└─────────────┘     └──────────────┘     └──────────────┘     └─────────────┘
                          ▲ ▼
                    ┌──────────────┐
                    │   MÉMOIRE    │
                    │  HISTORIQUE  │  ← articles, trends, reports
                    │   (SQLite)   │
                    └──────────────┘
       ▲
       │
  ┌──────────────┐
  │ GitHub Actions│  ← déclenche tout le pipeline, tourne dans le cloud
  │  (cron free) │
  └──────────────┘
```

---

## 5. Stack technique — 100 % gratuit

| Brique | Outil | Justification |
|---|---|---|
| **Langage** | Python 3.11+ | Standard IA, écosystème large |
| **LLM** | Groq API (Llama 3.1 / Mixtral) | Gratuit · rapide · API identique à OpenAI |
| **Collecte RSS** | `feedparser` | Python natif · 100 % gratuit |
| **Scraping** | `requests` + `BeautifulSoup` | Léger · gratuit · standard |
| **API Actualités** | GNews API (free tier) | 100 req/jour · sans restriction commerciale |
| **Stockage & mémoire** | SQLite (`sqlite3`) | Intégré Python · zéro configuration · mémoire historique |
| **Templates rapport** | Jinja2 | Gratuit · rendu HTML propre |
| **Scheduling & déploiement** | GitHub Actions (cron) | Gratuit · cloud · logs intégrés |
| **Email** | Gmail SMTP (`smtplib`) | Gratuit · natif Python |
| **Slack** | Incoming Webhooks Slack | Gratuit · une URL à configurer |
| **Logs** | `logging` (natif Python) | Gratuit · suffisant pour un MVP |

> ✅ **Aucun outil payant, aucune carte bancaire requise.** Tous les outils ci-dessus ont une version gratuite suffisante pour le volume d'un MVP sectoriel.

---

## 6. Contraintes du projet

| Contrainte | Description |
|---|---|
| **Zéro outil payant** | Stack 100 % gratuit — APIs free tier, bibliothèques open source |
| **Zéro intégration existante** | Tout est construit from scratch |
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

Le stage est découpé en **6 sprints de 2 semaines** (sur 12 semaines).

| Sprint | Semaines | Objectif | Tâches principales |
|---|---|---|---|
| **S1** | 1 – 2 | Fondations | Setup Python + GitHub, collecte RSS fonctionnelle, GNews API, schéma SQLite + mémoire historique |
| **S2** | 3 – 4 | Traitement | Filtrage mots-clés, déduplication via URL unique, alimentation table `trends`, logs |
| **S3** | 5 – 6 | Analyse IA | Intégration Groq API, prompts résumé, comparaison concurrentielle, contexte historique |
| **S4** | 7 – 8 | Rapport | Templates Jinja2, génération HTML, détection de tendances avec historique, opportunités |
| **S5** | 9 – 10 | Diffusion & Deploy | Gmail SMTP, Slack webhook, GitHub Actions cron, fichier de config |
| **S6** | 11 – 12 | Stabilisation | Tests end-to-end, gestion d'erreurs, documentation, démo finale |

---

## 9. Critères de succès du MVP

Le MVP est considéré comme livré si :

- [ ] L'agent s'exécute **de façon 100 % autonome** via GitHub Actions
- [ ] Un rapport est **généré et envoyé chaque semaine** sans action manuelle
- [ ] Le rapport contient : résumé exécutif, veille concurrentielle, tendances (avec contexte historique), opportunités
- [ ] Le pipeline tourne **sans interruption pendant 2 semaines consécutives**
- [ ] Le code est **déposé sur GitHub**, documenté, et repris possible par un tiers
- [ ] **Aucun outil payant** n'est utilisé dans la solution finale
- [ ] Le CEO valide la **pertinence des insights** produits par l'agent

---

## 10. Hors périmètre (MVP)

Les éléments suivants sont **exclus** et pourront être envisagés dans une version ultérieure :

- Interface utilisateur web ou dashboard de visualisation
- Personnalisation dynamique des rapports par l'utilisateur
- Surveillance de Twitter/X (API payante)
- Analyse des réseaux sociaux (restrictions légales et techniques)
- Alertes en temps réel (le MVP est hebdomadaire)
- Multi-langues (le MVP cible les sources en anglais)
- LLMs ou APIs payants

---

*Document rédigé dans le cadre du stage Welyne — Agent Sentinel (Veille Stratégique AI SaaS B2B)*
