#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GraphRAG 共用客戶端

把 MIS 後端所有原本散落在 rag_sys / learning_analytics 的「外部知識檢索」
集中走這支客戶端，下游程式只看到一致的 API。

設計原則：
1. 概念關聯查詢（細粒度結構）→ 直連 Neo4j 查 Concept schema，速度快
2. 自然語言檢索（粗粒度，含教材片段）→ HTTP 走 GRAPHRAG_API_BASE/retrieve
3. 概念名稱對不上時 → 自動模糊查找（先 exact、再 alias、再向量相近）

故意把所有錯誤都 swallow + 回空結構，讓上游邏輯不會因為 Neo4j/API 不通而崩潰。
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

# ============== 設定 ==============
GRAPHRAG_API_BASE = os.getenv("GRAPHRAG_API_BASE", "http://localhost:8001")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "123456789")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

# 預設超時（秒）
# /retrieve 內部要跑 embedding (Vertex AI) + Neo4j vector search + chain expansion，
# 第一次呼叫要 warm up model 常常會超過 60 秒，實測論文題目要 90-180 秒。
# 可用 env GRAPHRAG_QUERY_TIMEOUT 覆蓋（單位秒）。
DEFAULT_TIMEOUT = int(os.getenv("GRAPHRAG_DEFAULT_TIMEOUT", "30"))
QUERY_TIMEOUT = int(os.getenv("GRAPHRAG_QUERY_TIMEOUT", "180"))

_driver = None


def _build_driver():
    """建立 Neo4j driver，含 keep-alive 與較短的連線壽命，避免閒置斷線。"""
    return GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASS),
        max_connection_lifetime=300,        # 5 分鐘就丟掉重建
        connection_acquisition_timeout=10,
        keep_alive=True,
    )


def _get_driver():
    """共用 Neo4j driver（lazy init + 健康檢查 + 自動重連）。

    Neo4j Desktop / 防火牆閒置會把 Bolt 連線斷掉，舊 driver 內的 connection pool
    就會吐 `defunct connection` 例外。這裡每次都先 verify_connectivity()，失敗就重建。
    """
    global _driver
    # 第一次或上次失效
    if _driver is None:
        try:
            _driver = _build_driver()
            _driver.verify_connectivity()
        except Exception as e:
            logger.warning(f"⚠️ GraphRAG Neo4j 連線失敗: {e}")
            _driver = None
        return _driver

    # 已存在 → 健康檢查；失敗就重建
    try:
        _driver.verify_connectivity()
    except Exception as e:
        logger.warning(f"⚠️ GraphRAG Neo4j 連線失效，重建中: {e}")
        try:
            _driver.close()
        except Exception:
            pass
        try:
            _driver = _build_driver()
            _driver.verify_connectivity()
        except Exception as e2:
            logger.warning(f"⚠️ GraphRAG Neo4j 重建失敗: {e2}")
            _driver = None
    return _driver


# ============================================================
# 1. 概念名稱對齊（解決「micro_concept name 對不上 Concept.name」地雷）
# ============================================================
def resolve_concept_name(raw_name: str) -> Optional[str]:
    """把 MIS 的概念名稱對齊到 GraphRAG 圖譜中存在的 Concept.name。

    對齊順序：
        1. 完全相同
        2. 出現在 aliases 列表內
        3. 模糊比對：toLower CONTAINS
        4. 都找不到 → None
    """
    if not raw_name or not raw_name.strip():
        return None
    raw_name = raw_name.strip()

    driver = _get_driver()
    if driver is None:
        return None

    cypher = """
    MATCH (c:Concept)
    WITH c,
         [a IN coalesce(properties(c)['aliases'], [])
            WHERE toLower(a) = toLower($name)] AS exact_aliases,
         [a IN coalesce(properties(c)['aliases'], [])
            WHERE toLower(a) CONTAINS toLower($name)] AS partial_aliases
    WHERE toLower(c.name) = toLower($name)
       OR size(exact_aliases) > 0
       OR toLower(c.name) CONTAINS toLower($name)
       OR size(partial_aliases) > 0
    WITH c,
         CASE
           WHEN toLower(c.name) = toLower($name) THEN 0
           WHEN size(exact_aliases) > 0 THEN 1
           WHEN toLower(c.name) CONTAINS toLower($name) THEN 2
           ELSE 3
         END AS match_rank
    OPTIONAL MATCH (c)-[r]-()
    RETURN c.name AS name, match_rank, count(r) AS degree
    ORDER BY match_rank ASC, degree DESC, size(c.name) ASC
    LIMIT 1
    """
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            row = s.run(cypher, name=raw_name).single()
        if row and row.get("name"):
            resolved = row["name"]
            if resolved != raw_name:
                logger.info(f"🔗 [GraphRAG] 概念對齊: '{raw_name}' → '{resolved}'")
            return resolved
        logger.debug(f"⚠️ [GraphRAG] 概念對齊失敗: '{raw_name}' 不在圖譜中")
        return None
    except Exception as e:
        logger.error(f"❌ [GraphRAG] resolve_concept_name 失敗: {e}")
        return None


# ============================================================
# 2. 概念關聯查詢（取代原 get_knowledge_relations_from_neo4j）
# ============================================================
def get_concept_relations(concept_name: str) -> Dict[str, Any]:
    """從 GraphRAG 知識圖譜撈某個概念的前置 / 相關 / 後續 / 子類關係。

    回傳結構保持跟原本 learning_analytics.get_knowledge_relations_from_neo4j
    一致，下游 prompt 不用改：
    {
      "prerequisites":     [{name, type, strength, type_display, types: [...]}, ...],
      "related_concepts":  [...],   ← 同層 (HAS_SUBTYPE 同父 / 父概念) 視為相關
      "leads_to":          [...],
      "all_relations":     [...],
      "relation_graph":    {current_concept, current_id, nodes, edges},
      "has_relations":     bool
    }
    """
    empty = {
        "prerequisites": [],
        "related_concepts": [],
        "leads_to": [],
        "all_relations": [],
        "relation_graph": {
            "current_concept": concept_name,
            "current_id": None,
            "nodes": [],
            "edges": [],
        },
        "has_relations": False,
    }

    if not concept_name:
        return empty

    resolved = resolve_concept_name(concept_name)
    if not resolved:
        logger.warning(f"⚠️ [GraphRAG] 概念「{concept_name}」不在圖譜中，回空關聯")
        return empty

    driver = _get_driver()
    if driver is None:
        return empty

    # 注意：*1..N 展開後 r1 / r2 是 List<Relationship>，不是 Path，
    # Neo4j 5.x 要用 size() 而不是 length()。舊版可能有支援 length()，
    # 但新版會噴 `Type mismatch: expected Path but was List<Relationship>`
    cypher = """
    MATCH (c:Concept {name: $name})

    // 前置（直接 + 兩跳）
    OPTIONAL MATCH (prereq:Concept)-[r1:PREREQUISITE_OF*1..2]->(c)
    WITH c, collect(DISTINCT {node: prereq, depth: size(r1)}) AS prereq_raw

    // 後續（直接 + 兩跳）
    OPTIONAL MATCH (c)-[r2:PREREQUISITE_OF*1..2]->(nxt:Concept)
    WITH c, prereq_raw, collect(DISTINCT {node: nxt, depth: size(r2)}) AS next_raw

    // 子類（c → sub）
    OPTIONAL MATCH (c)-[:HAS_SUBTYPE]->(sub:Concept)
    WITH c, prereq_raw, next_raw, collect(DISTINCT sub) AS subs

    // 父類（parent → c）
    OPTIONAL MATCH (parent:Concept)-[:HAS_SUBTYPE]->(c)
    WITH c, prereq_raw, next_raw, subs, collect(DISTINCT parent) AS parents

    // 同類 sibling（parent → c, parent → sibling）視為相關
    OPTIONAL MATCH (p:Concept)-[:HAS_SUBTYPE]->(c)
    OPTIONAL MATCH (p)-[:HAS_SUBTYPE]->(sibling:Concept)
      WHERE sibling.name <> c.name
    WITH c, prereq_raw, next_raw, subs, parents,
         collect(DISTINCT sibling)[..5] AS siblings

    RETURN c.name AS current_name,
           elementId(c) AS current_id,
           [x IN prereq_raw WHERE x.node IS NOT NULL] AS prereqs,
           [x IN next_raw WHERE x.node IS NOT NULL]   AS nexts,
           subs, parents, siblings
    """

    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            row = session.run(cypher, name=resolved).single()
    except Exception as e:
        logger.error(f"❌ [GraphRAG] 查詢概念關聯失敗: {e}")
        return empty

    if not row:
        return empty

    current_id = str(row["current_id"])
    relation_graph = {
        "current_concept": resolved,
        "current_id": current_id,
        "nodes": [],
        "edges": [],
    }

    def _strength_from_depth(depth: int, base: float) -> float:
        # 直接相連 1 跳給滿、兩跳衰減
        return round(max(0.4, base * (1.0 / max(1, depth))), 2)

    prerequisites: List[Dict[str, Any]] = []
    leads_to: List[Dict[str, Any]] = []
    related: List[Dict[str, Any]] = []
    all_relations: List[Dict[str, Any]] = []

    # 前置
    for item in (row["prereqs"] or []):
        n = item["node"]
        if n is None:
            continue
        depth = int(item.get("depth") or 1)
        strength = _strength_from_depth(depth, 0.9)
        rec = {
            "id": n.get("name"),
            "name": n.get("name"),
            "type": "PREREQUISITE_OF",
            "depth": depth,
            "strength": strength,
            "type_display": "前置知識點",
            "types": ["PREREQUISITE_OF"],
        }
        prerequisites.append(rec)
        all_relations.append(rec)
        relation_graph["nodes"].append(
            {"id": n.get("name"), "name": n.get("name"), "type": "PREREQUISITE_OF"})
        relation_graph["edges"].append(
            {"source": n.get("name"), "target": resolved,
             "type": "PREREQUISITE_OF", "strength": strength,
             "label": "前置知識點"})

    # 後續
    for item in (row["nexts"] or []):
        n = item["node"]
        if n is None:
            continue
        depth = int(item.get("depth") or 1)
        strength = _strength_from_depth(depth, 0.8)
        rec = {
            "id": n.get("name"),
            "name": n.get("name"),
            "type": "LEADS_TO",
            "depth": depth,
            "strength": strength,
            "type_display": "後續知識點",
            "types": ["LEADS_TO"],
        }
        leads_to.append(rec)
        all_relations.append(rec)
        relation_graph["nodes"].append(
            {"id": n.get("name"), "name": n.get("name"), "type": "LEADS_TO"})
        relation_graph["edges"].append(
            {"source": resolved, "target": n.get("name"),
             "type": "LEADS_TO", "strength": strength,
             "label": "後續知識點"})

    # 相關：父概念 + sibling + 子類
    for n in (row["parents"] or []):
        if n is None:
            continue
        rec = {
            "id": n.get("name"),
            "name": n.get("name"),
            "type": "HAS_PARENT",
            "strength": 0.75,
            "type_display": "父類概念",
            "types": ["HAS_SUBTYPE"],
        }
        related.append(rec)
        all_relations.append(rec)
        relation_graph["nodes"].append(
            {"id": n.get("name"), "name": n.get("name"), "type": "HAS_PARENT"})
        relation_graph["edges"].append(
            {"source": n.get("name"), "target": resolved,
             "type": "HAS_SUBTYPE", "strength": 0.75,
             "label": "父類概念"})

    for n in (row["siblings"] or []):
        if n is None:
            continue
        rec = {
            "id": n.get("name"),
            "name": n.get("name"),
            "type": "SIMILAR_TO",
            "strength": 0.6,
            "type_display": "同類相關概念",
            "types": ["SIMILAR_TO"],
        }
        related.append(rec)
        all_relations.append(rec)

    for n in (row["subs"] or []):
        if n is None:
            continue
        rec = {
            "id": n.get("name"),
            "name": n.get("name"),
            "type": "HAS_SUBTYPE",
            "strength": 0.7,
            "type_display": "子類概念",
            "types": ["HAS_SUBTYPE"],
        }
        related.append(rec)
        all_relations.append(rec)
        relation_graph["nodes"].append(
            {"id": n.get("name"), "name": n.get("name"), "type": "HAS_SUBTYPE"})
        relation_graph["edges"].append(
            {"source": resolved, "target": n.get("name"),
             "type": "HAS_SUBTYPE", "strength": 0.7,
             "label": "子類概念"})

    logger.info(
        f"📊 [GraphRAG] '{resolved}' 關聯: "
        f"前置={len(prerequisites)} 相關={len(related)} 後續={len(leads_to)}"
    )

    return {
        "prerequisites": prerequisites,
        "related_concepts": related,
        "leads_to": leads_to,
        "all_relations": all_relations,
        "relation_graph": relation_graph,
        "has_relations": len(all_relations) > 0,
    }


# ============================================================
# 3. 自然語言問答（取代原 enhance_prompt_with_knowledge）
# ============================================================
# ============================================================
# 【MOD 新增】Seed 名稱歸一化（PascalCase）+ 事後去重
# 目的：解決 "virtual memory" / "Virtual_Memory" / "virtualMemory"
# 被當成 3 個獨立 seed 的問題。
# 另外用「詞集 hash + Gemini 判定」處理「MemoryVirtual vs VirtualMemory」
# 這種詞序錯亂的情形。
# ============================================================

# Gemini 判定結果快取，避免同一對名字重複問 Gemini
# key = frozenset({canonical_a, canonical_b}), value = "SAME" | "DIFFERENT"
_ALIAS_GEMINI_CACHE: Dict[frozenset, str] = {}


def _canonicalize_concept_name(name: str) -> str:
    """把各種寫法統一成 PascalCase 用於去重比對。
    範例：
      'virtual memory' -> 'VirtualMemory'
      'Virtual_Memory' -> 'VirtualMemory'
      'virtualMemory'  -> 'VirtualMemory'
      'VIRTUAL MEMORY' -> 'VirtualMemory'
      'page-replacement' -> 'PageReplacement'
    """
    import re as _re
    if not name:
        return ""
    s = str(name).strip()
    # 1. 分隔符（空格 / 底線 / 連字號）統一成空白
    s = _re.sub(r'[_\-\s]+', ' ', s)
    # 2. camelCase / PascalCase 拆單字（在大寫字前插空白）
    s = _re.sub(r'(?<!^)(?=[A-Z][a-z])', ' ', s)
    # 3. 連續大寫縮寫也拆（例如 "TLBCache" -> "TLB Cache"）
    s = _re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', s)
    # 4. 全轉小寫再切成單字
    words = [w for w in s.lower().split() if w]
    # 5. 每個單字首字大寫，合併
    return ''.join(w.capitalize() for w in words)


def _word_set_key(name: str) -> frozenset:
    """把名字拆成單字集合(不管順序)，用來偵測「詞序錯亂」的可疑對。
    VirtualMemory 和 MemoryVirtual 會得到同一個 frozenset(['virtual','memory'])。
    """
    import re as _re
    s = str(name or "").strip()
    if not s:
        return frozenset()
    s = _re.sub(r'[_\-\s]+', ' ', s)
    s = _re.sub(r'(?<!^)(?=[A-Z][a-z])', ' ', s)
    s = _re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', s)
    return frozenset(w for w in s.lower().split() if w)


def _ask_gemini_alias(name_a: str, name_b: str) -> str:
    """問 Gemini 兩個名字是否指同一概念。回傳 'SAME' / 'DIFFERENT' / 'UNKNOWN'。
    - 有 cache 直接回，避免同對重複問
    - Gemini 失敗回 'UNKNOWN' (不合併，保守處理)
    """
    canon_a = _canonicalize_concept_name(name_a)
    canon_b = _canonicalize_concept_name(name_b)
    if not canon_a or not canon_b or canon_a == canon_b:
        return "SAME"
    cache_key = frozenset({canon_a, canon_b})
    if cache_key in _ALIAS_GEMINI_CACHE:
        return _ALIAS_GEMINI_CACHE[cache_key]
    prompt = (
        "以下兩個名詞在計算機領域是否指同一個概念？只回答 SAME 或 DIFFERENT，不要其他字。\n"
        f"A: {name_a}\n"
        f"B: {name_b}\n"
        "回答："
    )
    verdict = "UNKNOWN"
    try:
        # 延遲 import 避免載入循環
        from accessories import init_gemini
        model = init_gemini(model_name="gemini-2.5-flash")
        resp = model.generate_content(prompt)
        text = str(getattr(resp, "text", "") or "").strip().upper()
        if "SAME" in text and "DIFFERENT" not in text:
            verdict = "SAME"
        elif "DIFFERENT" in text:
            verdict = "DIFFERENT"
    except Exception as e:
        logger.warning(f"[GraphRAG] Gemini alias 判定失敗 ({name_a} vs {name_b}): {e}")
        verdict = "UNKNOWN"
    _ALIAS_GEMINI_CACHE[cache_key] = verdict
    logger.info(f"[GraphRAG] Alias 判定 {name_a} vs {name_b} → {verdict}")
    return verdict


def query_natural_language(
    question: str,
    top_k: int = 5,
    concept_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """走 GRAPHRAG_API_BASE/retrieve 拿到種子概念、圖鏈與教材片段。

    這個 MIS 客戶端只需要檢索結果，最終答案會由 MIS 端統一使用同一個
    回答模型產生。因此不可呼叫會額外執行一次回答模型的 ``/query``，否則
    GraphRAG 會比 ChromaDB 多一次隱藏的 LLM 呼叫，污染延遲與成本實驗。

    回傳結構（來自 ComputerScienceKG api_server）：
    {
      "answer":            "",                  # 相容舊呼叫端，固定為空
      "seed_concepts":     [name, ...],
      "expanded":          [{name, sample_chunks, prerequisites, leads_to, ...}, ...],
      "prereq_chains":     [{concept, ancestors: [...]}, ...],
      "descendant_chains": [{concept, descendants: [...]}, ...],
    }

    連線失敗時回空結構（不會丟例外）。
    """
    if not question or not question.strip():
        return {"answer": "", "seed_concepts": [], "expanded": []}

    if concept_hint:
        question = f"關於「{concept_hint}」這個概念：{question}"

    try:
        # 【MOD】over-fetch：向 /retrieve 要 top_k * 2 個 seed，
        # 後端去重同義 seed 後再切回 top_k，避免因大小寫/底線/camelCase
        # 造成同一概念被當成多個 seed 佔滿名額。
        _fetch_k = min(max(top_k * 2, top_k + 3), 20)
        r = requests.post(
            f"{GRAPHRAG_API_BASE}/retrieve",
            json={"question": question, "top_k": _fetch_k},
            timeout=QUERY_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json() or {}
        # /query 舊格式的 seed_concepts 是名稱字串；/retrieve 原生格式則是
        # VECTOR_SEARCH 命中物件（name/definition/category/score）。MIS 的
        # prompt builder 以名稱字串和 expanded.name 對齊，因此在邊界正規化，
        # 同時把完整向量命中保留在 seed_hits 供 trace / 論文稽核。
        raw_seed_hits = list(data.get("seed_concepts") or [])
        # Keep the vector order explicit and auditable whenever scores are present.
        if raw_seed_hits and all(isinstance(item, dict) for item in raw_seed_hits):
            raw_seed_hits.sort(
                key=lambda item: (-float(item.get("score") or 0.0), str(item.get("name") or ""))
            )
        normalized_seed_names = []
        seen_canonicals: Dict[str, str] = {}  # canonical -> first-seen 原始名字
        merged_seeds: List[Dict[str, str]] = []  # 【MOD】被合併的別名紀錄
        gemini_verdicts: List[Dict[str, str]] = []  # 【MOD】Gemini 判定紀錄
        # ★★ Stage 1: canonical 完全一致就直接合併（不問 Gemini，0 成本）
        for seed in raw_seed_hits:
            if isinstance(seed, dict):
                name = str(seed.get("name") or "").strip()
            else:
                name = str(seed or "").strip()
            if not name:
                continue
            canon = _canonicalize_concept_name(name)
            if not canon:
                continue
            if canon in seen_canonicals:
                merged_seeds.append({
                    "kept": seen_canonicals[canon],
                    "merged": name,
                    "canonical": canon,
                    "reason": "exact_canonical_match",
                })
                continue
            seen_canonicals[canon] = name
            normalized_seed_names.append(name)
            if len(normalized_seed_names) >= top_k * 2:
                break
        # ★★ Stage 2: 詞集相同但詞序不同 → 送 Gemini 判定
        # 例：VirtualMemory vs MemoryVirtual (frozenset 都是 {'virtual','memory'})
        i = 0
        while i < len(normalized_seed_names):
            key_i = _word_set_key(normalized_seed_names[i])
            if len(key_i) < 2:
                i += 1
                continue
            j = i + 1
            while j < len(normalized_seed_names):
                key_j = _word_set_key(normalized_seed_names[j])
                if key_i == key_j:
                    # 詞集相同，詞序可能亂 → Gemini 判定
                    verdict = _ask_gemini_alias(
                        normalized_seed_names[i], normalized_seed_names[j]
                    )
                    gemini_verdicts.append({
                        "a": normalized_seed_names[i],
                        "b": normalized_seed_names[j],
                        "verdict": verdict,
                    })
                    if verdict == "SAME":
                        merged_seeds.append({
                            "kept": normalized_seed_names[i],
                            "merged": normalized_seed_names[j],
                            "canonical": _canonicalize_concept_name(normalized_seed_names[i]),
                            "reason": "gemini_verified_same",
                        })
                        del normalized_seed_names[j]
                        continue  # 不 j+=1，因為 pop 了
                j += 1
            i += 1
        # ★★ Stage 3: 湊夠 top_k 就切
        normalized_seed_names = normalized_seed_names[:top_k]
        data["seed_hits"] = raw_seed_hits
        data["seed_concepts"] = normalized_seed_names
        data["seed_canonical_merges"] = merged_seeds
        data["seed_gemini_alias_verdicts"] = gemini_verdicts  # trace 用

        # Over-fetch is only a candidate-recovery mechanism. Never let the 6th+
        # Seed leak into graph expansion or prerequisite chains downstream.
        selected_keys = {_canonicalize_concept_name(name) for name in normalized_seed_names}
        data["expanded"] = [
            item for item in (data.get("expanded") or [])
            if isinstance(item, dict)
            and _canonicalize_concept_name(str(item.get("name") or "")) in selected_keys
        ]
        data["prereq_chains"] = [
            item for item in (data.get("prereq_chains") or [])
            if isinstance(item, dict)
            and _canonicalize_concept_name(str(item.get("concept") or "")) in selected_keys
        ]
        data["descendant_chains"] = [
            item for item in (data.get("descendant_chains") or [])
            if isinstance(item, dict)
            and _canonicalize_concept_name(str(item.get("concept") or "")) in selected_keys
        ]
        if merged_seeds:
            logger.info(
                f"🔀 [GraphRAG] Seed 去重合併 {len(merged_seeds)} 個變體："
                + ", ".join(
                    f"{m['merged']}→{m['kept']}({m['reason']})"
                    for m in merged_seeds[:3]
                )
                + ("..." if len(merged_seeds) > 3 else "")
            )
        # 舊呼叫端可能仍會讀 answer；檢索端點不產生答案，保留空欄位相容。
        data.setdefault("answer", "")
        logger.info(
            f"🌐 [GraphRAG] /retrieve 命中 seed_concepts={len(data.get('seed_concepts', []))}"
        )
        return data
    except requests.exceptions.RequestException as e:
        upstream_detail = ""
        response = getattr(e, "response", None)
        if response is not None:
            try:
                upstream_detail = str(response.text or "").strip()[:1000]
            except Exception:
                upstream_detail = ""
        error_message = str(e)
        if upstream_detail:
            error_message = f"{error_message}; upstream={upstream_detail}"
        logger.warning(
            f"⚠️ [GraphRAG] /retrieve 連線失敗: {e}（ComputerScienceKG api_server 是否啟動？）"
        )
        return {
            "answer": "",
            "seed_concepts": [],
            "expanded": [],
            "prereq_chains": [],
            "descendant_chains": [],
            "error": error_message,
        }


def fetch_concept_snippets(concept_name: str, max_snippets: int = 3) -> List[str]:
    """撈某個概念在教材中的原文片段（取代 ChromaDB 教材檢索）。

    用於把「圖譜檢索結果」變回類似純 RAG 的教材片段，讓引導式對話的
    prompt 仍能提供「教材原文」素材。
    """
    if not concept_name:
        return []

    resolved = resolve_concept_name(concept_name)
    if not resolved:
        return []

    driver = _get_driver()
    if driver is None:
        return []

    cypher = """
    MATCH (c:Concept {name: $name})-[mention_relation]-(k:Chunk)
    WHERE type(mention_relation) IN ['MENTIONED_IN', 'MENTIONS']
    RETURN k.text AS text
    ORDER BY coalesce(
      properties(k)['chunkSeqId'],
      properties(k)['pageStart'],
      properties(k)['chunkId']
    ) ASC
    LIMIT $limit
    """
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            rows = s.run(cypher, name=resolved, limit=max_snippets).data()
        snippets = [r["text"] for r in rows if r.get("text")]
        logger.debug(
            f"📖 [GraphRAG] '{resolved}' 抓到 {len(snippets)} 個教材片段"
        )
        return snippets
    except Exception as e:
        logger.error(f"❌ [GraphRAG] fetch_concept_snippets 失敗: {e}")
        return []


def fetch_concept_profile(
    concept_name: str,
    max_snippets: int = 1,
    relation_limit: int = 12,
    include_leads_to: bool = True,
    include_context_relations: bool = True,
) -> Dict[str, Any]:
    """撈單一 Concept 的 prompt 用資料。

    這支比 fetch_concept_snippets 更完整：除了 Chunk，也會帶出定義、
    直接先備、父類、子類；可選擇是否包含直接後續。GraphRAG 補救學習
    會用 include_leads_to=False，避免先備概念再往下擴張造成 prompt 膨脹。
    """
    empty = {
        "name": concept_name,
        "definition": "",
        "prerequisites": [],
        "leads_to": [],
        "parents": [],
        "subtypes": [],
        "sample_chunks": [],
        "sample_chunk_records": [],
    }
    if not concept_name:
        return empty

    resolved = resolve_concept_name(concept_name)
    if not resolved:
        return empty

    driver = _get_driver()
    if driver is None:
        return empty

    if not include_context_relations:
        cypher = """
        MATCH (c:Concept {name: $name})
        CALL (c) {
          MATCH (c)-[mention_relation]-(k:Chunk)
          WHERE type(mention_relation) IN ['MENTIONED_IN', 'MENTIONS']
          WITH k
          ORDER BY coalesce(
            properties(k)['chunkSeqId'],
            properties(k)['pageStart'],
            properties(k)['chunkId']
          ) ASC
          RETURN collect({
            text: coalesce(k.text, ""),
            chunk_seq_id: properties(k)['chunkSeqId'],
            chunk_id: coalesce(k.chunkId, ""),
            source: coalesce(properties(k)['bookId'], ""),
            book_id: coalesce(properties(k)['bookId'], ""),
            chapter_id: coalesce(properties(k)['chapterId'], ""),
            page_start: k.pageStart,
            page_end: k.pageEnd,
            char_start: properties(k)['charStart'],
            char_end: properties(k)['charEnd'],
            text_hash: coalesce(properties(k)['textHash'], "")
          }) AS chunk_records
        }
        RETURN c.name AS name,
               coalesce(c.definition, "") AS definition,
               [] AS prerequisites,
               [] AS leads_to,
               [] AS parents,
               [] AS subtypes,
               chunk_records
        """
    else:
        cypher = """
    MATCH (c:Concept {name: $name})

    OPTIONAL MATCH (prereq:Concept)-[:PREREQUISITE_OF]->(c)
    WITH c, collect(DISTINCT prereq.name) AS prerequisites

    OPTIONAL MATCH (c)-[:PREREQUISITE_OF]->(nxt:Concept)
    WITH c, prerequisites, collect(DISTINCT nxt.name) AS leads_to

    OPTIONAL MATCH (parent:Concept)-[:HAS_SUBTYPE]->(c)
    WITH c, prerequisites, leads_to, collect(DISTINCT parent.name) AS parents

    OPTIONAL MATCH (c)-[:HAS_SUBTYPE]->(sub:Concept)
    WITH c, prerequisites, leads_to, parents, collect(DISTINCT sub.name) AS subtypes

    CALL (c) {
      MATCH (c)-[mention_relation]-(k:Chunk)
      WHERE type(mention_relation) IN ['MENTIONED_IN', 'MENTIONS']
      WITH k
      ORDER BY coalesce(
        properties(k)['chunkSeqId'],
        properties(k)['pageStart'],
        properties(k)['chunkId']
      ) ASC
      RETURN collect({
        text: coalesce(k.text, ""),
        chunk_seq_id: properties(k)['chunkSeqId'],
        chunk_id: coalesce(k.chunkId, ""),
        source: coalesce(properties(k)['bookId'], ""),
        book_id: coalesce(properties(k)['bookId'], ""),
        chapter_id: coalesce(properties(k)['chapterId'], ""),
        page_start: k.pageStart,
        page_end: k.pageEnd,
        char_start: properties(k)['charStart'],
        char_end: properties(k)['charEnd'],
        text_hash: coalesce(properties(k)['textHash'], "")
      }) AS chunk_records
    }

    RETURN c.name AS name,
           coalesce(c.definition, "") AS definition,
           prerequisites,
           leads_to,
           parents,
           subtypes,
           chunk_records
    """
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            row = s.run(cypher, name=resolved).single()
        if not row:
            return empty

        def _clean_names(values: Any) -> List[str]:
            cleaned: List[str] = []
            seen = set()
            for value in values or []:
                name = str(value or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                cleaned.append(name)
            return cleaned[: max(1, relation_limit)]

        raw_chunk_records = list(row.get("chunk_records") or row.get("chunks") or [])
        chunk_records: List[Dict[str, Any]] = []
        for value in raw_chunk_records:
            if isinstance(value, dict):
                record = dict(value)
                text = str(record.get("text") or "")
            else:
                text = str(value or "")
                record = {"text": text}
            if not text:
                continue
            record["text"] = text
            chunk_records.append(record)
            if len(chunk_records) >= max(1, max_snippets):
                break

        profile = {
            "name": row.get("name") or resolved,
            "definition": row.get("definition") or "",
            "prerequisites": _clean_names(row.get("prerequisites")),
            "leads_to": _clean_names(row.get("leads_to")) if include_leads_to else [],
            "parents": _clean_names(row.get("parents")),
            "subtypes": _clean_names(row.get("subtypes")),
            "sample_chunks": [record["text"] for record in chunk_records],
            "sample_chunk_records": chunk_records,
        }
        logger.debug(
            f"📚 [GraphRAG] profile '{resolved}': "
            f"prereq={len(profile['prerequisites'])} "
            f"parents={len(profile['parents'])} "
            f"subtypes={len(profile['subtypes'])} "
            f"chunks={len(profile['sample_chunks'])}"
        )
        return profile
    except Exception as e:
        logger.error(f"❌ [GraphRAG] fetch_concept_profile 失敗: {e}")
        return empty


_GENERIC_NEXT_CONCEPT_NAMES = {
    "system",
    "systems",
    "data",
    "information",
    "user",
    "page",
    "pages",
    "process",
    "method",
    "methods",
    "problem",
    "problems",
    "example",
    "examples",
    "concept",
    "concepts",
    "node",
    "nodes",
    "algorithm",
    "algorithms",
    "structure",
    "structures",
    "management",
}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 1) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _compact_concept_name(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _generic_concept_penalty(name: str) -> float:
    compact = _compact_concept_name(name)
    if not compact:
        return 10.0
    if compact in _GENERIC_NEXT_CONCEPT_NAMES:
        return 8.0
    if len(compact) <= 3:
        return 4.0
    return 0.0


def select_next_concept(
    concept_name: str,
    mastered_concepts: Optional[List[str]] = None,
    question_text: str = "",
    max_candidates: int = 8,
) -> Dict[str, Any]:
    """Select one downstream concept for remedial learning.

    The LLM explains the selected concept; it should not choose the next
    concept by itself.  Selection rules:
      1. ignore invalid / negative relations;
      2. ignore already mastered concepts;
      3. prefer direct downstream concepts;
      4. prefer concepts that have definition and real Chunk material.
    """
    resolved = resolve_concept_name(concept_name)
    result = {
        "source_concept": resolved or concept_name,
        "selected": None,
        "candidates": [],
        "reason": "no_source_concept",
    }
    if not resolved:
        return result

    mastered = {
        _compact_concept_name(name)
        for name in (mastered_concepts or [])
        if str(name or "").strip()
    }
    relations = get_concept_relations(resolved)
    leads_to = relations.get("leads_to") or []
    if not leads_to:
        result["reason"] = "no_downstream_concepts"
        return result

    direct_leads = [
        rel for rel in leads_to
        if max(1, _as_int(rel.get("depth"), 1)) == 1
    ]
    if not direct_leads:
        result["reason"] = "no_direct_downstream_concepts"
        return result
    leads_to = direct_leads

    def _pre_rank_relation(rel: Dict[str, Any]) -> tuple:
        strength = _as_float(
            rel.get("strength", rel.get("score", rel.get("confidence", 0.0))),
            0.0,
        )
        return (
            max(1, _as_int(rel.get("depth"), 99)),
            -max(strength, _as_float(rel.get("confidence"), strength)),
        )

    # Avoid walking every downstream concept when the graph is dense.
    leads_to = sorted(leads_to, key=_pre_rank_relation)[: max(6, max_candidates * 4)]

    q_lower = (question_text or "").lower()
    candidates: List[Dict[str, Any]] = []
    seen = set()

    for rel in leads_to:
        name = str(rel.get("name") or rel.get("id") or "").strip()
        if not name:
            continue
        compact = _compact_concept_name(name)
        if not compact or compact in seen:
            continue
        seen.add(compact)
        if compact in mastered:
            continue

        strength = _as_float(
            rel.get("strength", rel.get("score", rel.get("confidence", 0.0))),
            0.0,
        )
        confidence = _as_float(rel.get("confidence"), strength)
        relation_score = max(strength, confidence)
        if relation_score < 0:
            continue

        depth = max(1, _as_int(rel.get("depth"), 1))
        profile = fetch_concept_profile(
            name,
            max_snippets=1,
            relation_limit=0,
            include_leads_to=False,
            include_context_relations=False,
        )
        definition = str(profile.get("definition") or "").strip()
        chunks = [str(s).strip() for s in (profile.get("sample_chunks") or []) if str(s).strip()]
        has_definition = bool(definition)
        has_chunk = bool(chunks)

        rank_score = 0.0
        rank_score += relation_score * 10.0
        rank_score += 6.0 if depth == 1 else 2.0 / depth
        rank_score += 5.0 if has_chunk else 0.0
        rank_score += 2.0 if has_definition else 0.0
        if name.lower() in q_lower:
            rank_score += 2.0
        penalty = _generic_concept_penalty(name)
        rank_score -= penalty

        reasons = []
        reasons.append("直接後續概念" if depth == 1 else f"{depth} 層後續概念")
        if has_chunk:
            reasons.append("有教材 Chunk")
        if has_definition:
            reasons.append("有定義")
        if penalty > 0:
            reasons.append("名稱偏泛稱，已降權")

        candidates.append({
            "name": name,
            "relation": f"{resolved} PREREQUISITE_OF {name}",
            "depth": depth,
            "strength": strength,
            "confidence": confidence,
            "rank_score": round(rank_score, 4),
            "has_chunk": has_chunk,
            "has_definition": has_definition,
            "definition": definition,
            "chunk_preview": chunks[0][:500] if chunks else "",
            "reason": "；".join(reasons),
        })

    candidates.sort(
        key=lambda item: (
            item.get("rank_score", 0),
            item.get("has_chunk", False),
            item.get("has_definition", False),
            -item.get("depth", 99),
            item.get("strength", 0),
        ),
        reverse=True,
    )

    result["candidates"] = candidates[: max(1, max_candidates)]
    if candidates:
        result["selected"] = candidates[0]
        result["reason"] = "selected_best_downstream"
    else:
        result["reason"] = "no_valid_downstream_after_filters"
    return result


# ============================================================
# STRICT RESEARCH POLICY
# ============================================================
# 目前論文/實驗固定規則：
#   1) 完整原始題目 -> Top-5 Seed Concepts
#   2) 每個 Seed 的所有直接 Chunk 以原題排序，各取 Top-3；五組合併後去重
#   3) 只找五個 Seed 的 distance=1 PREREQUISITE_OF 直接先備 Concept
#   4) 所有直接先備 Concept 去重後，以原題全域排序取 Top-5
#   5) 先備 Chunk 的候選池來自「所有 distance=1 先備 Concept」，不是只來自 Concept Top-5
#   6) 所有先備 Chunk 先合併、去重，再以原題全域排序取 Top-5
# 所有語意排名都透過 ComputerScienceKG /semantic-scores，與 Concept 向量檢索共用
# 同一個 GeminiEmbedder；若該端點失敗，研究模式 fail-closed，不偷偷改用其他模型。
RESEARCH_SEED_TOP_K = 5
RESEARCH_SEED_CHUNKS_PER_CONCEPT = 3
RESEARCH_PREREQ_CONCEPT_TOP_K = 5
RESEARCH_PREREQ_CHUNK_TOP_K = 5
# Bump whenever the evidence-selection contract changes. One-shot tutoring
# sessions use this value to invalidate a context made under an older policy.
STRICT_RESEARCH_POLICY_VERSION = "strict_research_policy_v2"


def _strict_chunk_identity(record: Dict[str, Any]) -> str:
    """Exact-content identity for within-pool dedup.

    The thesis says Chunk deduplication, so identical complete text must collapse even
    when two imports gave it different chunk IDs. chunk_id is only a fallback when
    text is unavailable.
    """
    text = str(record.get("text") or record.get("raw_content") or "")
    if text:
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    chunk_id = str(record.get("chunk_id") or record.get("chunkId") or "").strip()
    return f"id:{chunk_id}" if chunk_id else "empty:"


def _strict_semantic_scores(question: str, texts: Sequence[str]) -> Tuple[List[float], Dict[str, Any]]:
    """Score all texts against the complete original question using upstream GeminiEmbedder.

    This intentionally has no lexical/local-embedding fallback. A fallback would make the
    experiment use a different retrieval method without being visible in the trace.
    """
    values = [str(value or "") for value in texts]
    if not values:
        return [], {"backend": "not_run", "count": 0}
    r = requests.post(
        f"{GRAPHRAG_API_BASE}/semantic-scores",
        json={"query": str(question or ""), "texts": values},
        timeout=QUERY_TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json() or {}
    raw_scores = payload.get("scores") or []
    if len(raw_scores) != len(values):
        raise RuntimeError(
            f"semantic-scores length mismatch: expected {len(values)}, got {len(raw_scores)}"
        )
    scores = [max(-1.0, min(1.0, float(score))) for score in raw_scores]
    return scores, {
        "backend": payload.get("backend") or "ComputerScienceKG/GeminiEmbedder",
        "model": payload.get("model"),
        "vector_length": payload.get("vector_length"),
        "count": len(scores),
    }


def _strict_vector_seed_hits(question: str, top_k: int = RESEARCH_SEED_TOP_K) -> Dict[str, Any]:
    """Return exactly the vector-ranked Top-K Concept nodes, with no graph expansion."""
    top_k = max(1, min(int(top_k or RESEARCH_SEED_TOP_K), RESEARCH_SEED_TOP_K))
    r = requests.post(
        f"{GRAPHRAG_API_BASE}/retrieve-seeds",
        json={"question": str(question or ""), "top_k": top_k},
        timeout=QUERY_TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json() or {}
    hits = [dict(item) for item in (payload.get("seed_concepts") or []) if isinstance(item, dict)]
    # Be explicit even though Neo4j vector query already yields nearest-neighbour order.
    hits.sort(key=lambda item: (-float(item.get("score") or 0.0), str(item.get("name") or "")))
    seen = set()
    selected = []
    for item in hits:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= top_k:
            break
    return {
        "hits": selected,
        "trace": payload.get("retrieval_trace") or {},
    }


def fetch_all_concept_chunk_records(concept_name: str) -> List[Dict[str, Any]]:
    """Fetch every distinct Chunk directly linked to one exact Concept. No Top-N truncation."""
    name = str(concept_name or "").strip()
    if not name:
        return []
    driver = _get_driver()
    if driver is None:
        return []
    cypher = """
    MATCH (c:Concept {name: $name})-[mention_relation]-(k:Chunk)
    WHERE type(mention_relation) IN ['MENTIONED_IN', 'MENTIONS']
    WITH DISTINCT k
    RETURN coalesce(k.chunkId, '') AS chunk_id,
           coalesce(k.text, '') AS text,
           coalesce(properties(k)['bookId'], '') AS book_id,
           coalesce(properties(k)['chapterId'], '') AS chapter_id,
           properties(k)['chunkSeqId'] AS chunk_seq_id,
           coalesce(properties(k)['pageStart'], properties(k)['page']) AS page_start,
           properties(k)['pageEnd'] AS page_end,
           properties(k)['charStart'] AS char_start,
           properties(k)['charEnd'] AS char_end,
           coalesce(properties(k)['textHash'], '') AS text_hash
    ORDER BY coalesce(properties(k)['chunkSeqId'], properties(k)['pageStart'], properties(k)['page'], 0) ASC,
             coalesce(k.chunkId, '') ASC
    """
    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            rows = session.run(cypher, name=name).data()
    except Exception as exc:
        logger.error("❌ [GraphRAG] fetch_all_concept_chunk_records(%s) 失敗: %s", name, exc)
        return []
    out = []
    for row in rows:
        text = str(row.get("text") or "")
        if not text.strip():
            continue
        rec = dict(row)
        rec["concept"] = name
        rec["source"] = rec.get("book_id") or ""
        rec["identity"] = _strict_chunk_identity(rec)
        out.append(rec)
    return out


def fetch_all_direct_prerequisite_candidates(seed_concepts: Sequence[str]) -> List[Dict[str, Any]]:
    """All unique distance-1 (prereq)-[:PREREQUISITE_OF]->(seed) candidates."""
    seed_names = []
    seen_seed = set()
    for value in seed_concepts:
        name = str(value or "").strip()
        if name and name.casefold() not in seen_seed:
            seen_seed.add(name.casefold())
            seed_names.append(name)
    if not seed_names:
        return []
    driver = _get_driver()
    if driver is None:
        return []
    cypher = """
    UNWIND $seed_names AS seed_name
    MATCH (seed:Concept {name: seed_name})
    MATCH (prereq:Concept)-[:PREREQUISITE_OF]->(seed)
    WITH prereq, collect(DISTINCT seed.name) AS source_seeds
    RETURN prereq.name AS name,
           coalesce(prereq.definition, '') AS definition,
           source_seeds
    ORDER BY prereq.name ASC
    """
    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            rows = session.run(cypher, seed_names=seed_names).data()
    except Exception as exc:
        logger.error("❌ [GraphRAG] distance=1 直接先備查詢失敗: %s", exc)
        return []
    rank_map = {name: idx + 1 for idx, name in enumerate(seed_names)}
    out = []
    seen = set()
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        source_seeds = sorted(
            {str(value or "").strip() for value in (row.get("source_seeds") or []) if str(value or "").strip()},
            key=lambda value: rank_map.get(value, 999999),
        )
        out.append({
            "name": name,
            "definition": str(row.get("definition") or ""),
            "source_seed_concepts": source_seeds,
            "source_seed_ranks": [rank_map.get(value) for value in source_seeds],
            "graph_distance": 1,
            "relation": "PREREQUISITE_OF",
        })
    return out


def _merge_duplicate_chunk_record(kept: Dict[str, Any], incoming: Dict[str, Any], provenance_key: str) -> None:
    values: List[str] = []
    for container in (kept.get(provenance_key), incoming.get(provenance_key)):
        if container is None:
            continue
        sequence = container if isinstance(container, (list, tuple, set)) else [container]
        for source in sequence:
            value = str(source or "").strip()
            if value and value not in values:
                values.append(value)
    kept[provenance_key] = values


def _strict_score_value(value: Any, default: float = -1.0) -> float:
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def retrieve_research_policy(question: str) -> Dict[str, Any]:
    """Execute the exact thesis retrieval policy requested by the user."""
    original_question = str(question or "").strip()
    empty = {
        "question": original_question,
        "seed_concepts": [],
        "seed_hits": [],
        "seed_chunks": [],
        "all_prerequisite_concepts": [],
        "prerequisite_concepts": [],
        "prerequisite_chunks": [],
        "expanded": [],
        "prereq_chains": [],
        "descendant_chains": [],
        "retrieval_trace": {"policy": STRICT_RESEARCH_POLICY_VERSION},
    }
    if not original_question:
        return empty
    try:
        # A. Full question -> vector Top-5 Seed Concepts.
        seed_result = _strict_vector_seed_hits(original_question, RESEARCH_SEED_TOP_K)
        seed_hits = seed_result["hits"]
        seed_names = [str(hit.get("name") or "").strip() for hit in seed_hits if str(hit.get("name") or "").strip()]
        empty["seed_hits"] = seed_hits
        empty["seed_concepts"] = seed_names
        empty["retrieval_trace"]["seed_vector_search"] = seed_result.get("trace") or {}
        if not seed_names:
            return empty

        # B. Each Seed -> ALL linked chunks -> semantic rank by original question -> Top-3 each.
        provisional_seed_chunks: List[Dict[str, Any]] = []
        seed_profiles = []
        semantic_traces = []
        for seed_rank, seed_name in enumerate(seed_names, start=1):
            records = fetch_all_concept_chunk_records(seed_name)
            texts = [record["text"] for record in records]
            scores, score_trace = _strict_semantic_scores(original_question, texts) if records else ([], {"backend": "not_run", "count": 0})
            semantic_traces.append({"stage": "seed_chunk", "concept": seed_name, **score_trace})
            for record, score in zip(records, scores):
                record["semantic_score"] = float(score)
                record["source_seed_concepts"] = [seed_name]
                record["source_seed_rank"] = seed_rank
            ranked = sorted(
                records,
                key=lambda record: (
                    -_strict_score_value(record.get("semantic_score")),
                    str(record.get("chunk_id") or ""),
                ),
            )
            selected = ranked[:RESEARCH_SEED_CHUNKS_PER_CONCEPT]
            for local_rank, record in enumerate(selected, start=1):
                record["seed_chunk_rank"] = local_rank
                record["retrieval_stage"] = "seed"
                provisional_seed_chunks.append(record)
            seed_profiles.append({
                "name": seed_name,
                "definition": next((str(hit.get("definition") or "") for hit in seed_hits if str(hit.get("name") or "").strip() == seed_name), ""),
                "all_chunk_count": len(records),
                "selected_chunk_count": len(selected),
            })

        # Merge the at-most-15 selected Seed chunks, then dedup globally. Do NOT refill.
        seed_unique: Dict[str, Dict[str, Any]] = {}
        for record in provisional_seed_chunks:
            key = _strict_chunk_identity(record)
            if key not in seed_unique:
                kept = dict(record)
                kept["identity"] = key
                seed_unique[key] = kept
            else:
                _merge_duplicate_chunk_record(seed_unique[key], record, "source_seed_concepts")
        seed_chunks = list(seed_unique.values())
        seed_chunks.sort(key=lambda record: (int(record.get("source_seed_rank") or 999999), int(record.get("seed_chunk_rank") or 999999), -float(record.get("semantic_score") or 0.0)))

        # C. All distance-1 prerequisites from the five Seeds.
        all_prereqs = fetch_all_direct_prerequisite_candidates(seed_names)
        concept_texts = [
            ". ".join(part for part in (str(item.get("name") or "").strip(), str(item.get("definition") or "").strip()) if part)
            for item in all_prereqs
        ]
        prereq_scores, prereq_score_trace = _strict_semantic_scores(original_question, concept_texts) if all_prereqs else ([], {"backend": "not_run", "count": 0})
        semantic_traces.append({"stage": "prerequisite_concept", **prereq_score_trace})
        for item, score in zip(all_prereqs, prereq_scores):
            item["semantic_score"] = float(score)
        ranked_prereqs = sorted(
            all_prereqs,
            key=lambda item: (-_strict_score_value(item.get("semantic_score")), str(item.get("name") or "")),
        )
        selected_prereqs = []
        for rank, item in enumerate(ranked_prereqs[:RESEARCH_PREREQ_CONCEPT_TOP_K], start=1):
            selected = dict(item)
            selected["global_prerequisite_rank"] = rank
            selected_prereqs.append(selected)

        # D. Only the semantic Top-5 D1 Concepts may contribute prerequisite
        #    Chunks. This preserves the hard five-concept teaching contract.
        #    Collect their chunks -> dedup first -> semantic rank -> Top-5.
        prereq_chunk_unique: Dict[str, Dict[str, Any]] = {}
        total_prereq_chunk_rows = 0
        for prereq in selected_prereqs:
            prereq_name = str(prereq.get("name") or "").strip()
            for record in fetch_all_concept_chunk_records(prereq_name):
                total_prereq_chunk_rows += 1
                record = dict(record)
                record["retrieval_stage"] = "prerequisite"
                record["graph_distance"] = 1
                record["source_prerequisite_concepts"] = [prereq_name]
                record["source_seed_concepts"] = list(prereq.get("source_seed_concepts") or [])
                key = _strict_chunk_identity(record)
                if key not in prereq_chunk_unique:
                    record["identity"] = key
                    prereq_chunk_unique[key] = record
                else:
                    kept = prereq_chunk_unique[key]
                    _merge_duplicate_chunk_record(kept, record, "source_prerequisite_concepts")
                    _merge_duplicate_chunk_record(kept, record, "source_seed_concepts")
        prereq_unique_records = list(prereq_chunk_unique.values())
        prereq_chunk_scores, prereq_chunk_trace = _strict_semantic_scores(
            original_question, [record["text"] for record in prereq_unique_records]
        ) if prereq_unique_records else ([], {"backend": "not_run", "count": 0})
        semantic_traces.append({"stage": "prerequisite_chunk", **prereq_chunk_trace})
        for record, score in zip(prereq_unique_records, prereq_chunk_scores):
            record["semantic_score"] = float(score)
        prereq_unique_records.sort(
            key=lambda record: (-_strict_score_value(record.get("semantic_score")), str(record.get("chunk_id") or ""))
        )
        prerequisite_chunks = []
        for rank, record in enumerate(prereq_unique_records[:RESEARCH_PREREQ_CHUNK_TOP_K], start=1):
            selected = dict(record)
            selected["global_prerequisite_chunk_rank"] = rank
            prerequisite_chunks.append(selected)

        empty.update({
            "seed_chunks": seed_chunks,
            "all_prerequisite_concepts": ranked_prereqs,
            "prerequisite_concepts": selected_prereqs,
            "prerequisite_chunks": prerequisite_chunks,
            "expanded": seed_profiles,
            "prereq_chains": [
                {
                    "concept": seed_name,
                    "ancestors": [
                        {"name": item["name"], "definition": item.get("definition", ""), "distance": 1}
                        for item in ranked_prereqs
                        if seed_name in (item.get("source_seed_concepts") or [])
                    ],
                }
                for seed_name in seed_names
            ],
        })
        empty["retrieval_trace"].update({
            "policy": STRICT_RESEARCH_POLICY_VERSION,
            "query_basis": "complete_original_question",
            "seed_top_k": RESEARCH_SEED_TOP_K,
            "seed_chunks_per_concept": RESEARCH_SEED_CHUNKS_PER_CONCEPT,
            "seed_chunk_pre_dedup_count": len(provisional_seed_chunks),
            "seed_chunk_post_dedup_count": len(seed_chunks),
            "prerequisite_relation": "PREREQUISITE_OF",
            "prerequisite_distance": 1,
            "all_d1_prerequisite_count": len(all_prereqs),
            "prerequisite_concept_top_k": RESEARCH_PREREQ_CONCEPT_TOP_K,
            "selected_d1_prerequisite_count": len(selected_prereqs),
            "prerequisite_chunk_candidate_rows": total_prereq_chunk_rows,
            "prerequisite_chunk_post_dedup_count": len(prereq_unique_records),
            "prerequisite_chunk_top_k": RESEARCH_PREREQ_CHUNK_TOP_K,
            "prerequisite_chunk_source_scope": "selected_top5_distance_1_prerequisite_concepts",
            "semantic_scoring": semantic_traces,
        })
        return empty
    except Exception as exc:
        logger.exception("❌ [GraphRAG] strict research policy retrieval failed: %s", exc)
        empty["error"] = f"{type(exc).__name__}: {exc}"
        return empty


# ============================================================
# 4. 漸進式學習階段素材（給 build_followup_prompt 用）
# ============================================================
def fetch_stage_materials(
    concept_name: str,
    stage: str,
) -> Dict[str, Any]:
    """根據漸進式學習階段，撈對應的圖譜素材給 prompt。

    這是漸進式學習 × GraphRAG 整合的核心：
        - core_concept_confirmation   → 前置概念 + 父類（從基礎切入）
        - related_concept_guidance    → 同層 sibling + 子類（建立概念網絡）
        - application_understanding   → 後續概念（拿應用情境）
        - understanding_verification  → 完整子圖（讓 AI 驗證學生講得對不對）
        - completed                   → 後續可學概念清單（指引下一步）

    回傳：
    {
      "stage": str,
      "focus_concepts": [{name, definition}, ...],  ← 該階段重點素材
      "guidance_hint":  str,                        ← 「這階段你該怎麼用這些素材」
      "snippets":       [str, ...],                 ← 對應的教材原文
    }
    """
    empty = {
        "stage": stage,
        "focus_concepts": [],
        "guidance_hint": "",
        "snippets": [],
    }
    if not concept_name:
        return empty

    relations = get_concept_relations(concept_name)
    if not relations["has_relations"]:
        return empty

    focus_concepts: List[Dict[str, Any]] = []
    guidance_hint = ""
    direct_prerequisites = [
        r for r in relations["prerequisites"]
        if max(1, _as_int(r.get("depth"), 1)) == 1
    ]
    direct_leads_to = [
        r for r in relations["leads_to"]
        if max(1, _as_int(r.get("depth"), 1)) == 1
    ]

    if stage == "core_concept_confirmation":
        # 從前置概念切入，找學生最弱的基礎點
        focus_concepts = [
            {"name": r["name"], "role": "前置", "strength": r["strength"]}
            for r in direct_prerequisites[:3]
        ]
        guidance_hint = (
            "本階段請優先針對下列前置概念提問，確認學生基礎是否扎實。"
            "若學生對某前置概念回答不清楚，立刻先把該前置概念補齊。"
        )
    elif stage == "related_concept_guidance":
        # 用相關 / 子類概念建立概念網絡
        focus_concepts = [
            {"name": r["name"], "role": "相關", "strength": r["strength"]}
            for r in relations["related_concepts"][:4]
        ]
        guidance_hint = (
            "本階段請延伸提問下列相關概念，協助學生建立概念之間的連結，"
            "可使用「A 與 B 的差異」「A 屬於 B 的哪一種」這類問題。"
        )
    elif stage == "application_understanding":
        # 用後續概念當作應用情境
        focus_concepts = [
            {"name": r["name"], "role": "後續/應用", "strength": r["strength"]}
            for r in direct_leads_to[:3]
        ]
        guidance_hint = (
            "本階段請以下列後續/應用概念出題，讓學生把當前概念應用到具體情境，"
            "看出學生是否能遷移知識。"
        )
    elif stage == "understanding_verification":
        # 反向教導：用所有關聯讓學生重新講一遍
        focus_concepts = (
            [{"name": r["name"], "role": "前置", "strength": r["strength"]}
             for r in direct_prerequisites[:2]]
            + [{"name": r["name"], "role": "相關", "strength": r["strength"]}
               for r in relations["related_concepts"][:2]]
            + [{"name": r["name"], "role": "後續", "strength": r["strength"]}
               for r in direct_leads_to[:2]]
        )
        guidance_hint = (
            "本階段請要求學生用自己的話解釋「當前概念」與下列關聯概念的關係，"
            "若解釋不清楚就直接糾正並補充正確說明。"
        )
    elif stage == "completed":
        focus_concepts = [
            {"name": r["name"], "role": "可接續學習", "strength": r["strength"]}
            for r in direct_leads_to[:5]
        ]
        guidance_hint = (
            "學生已掌握當前概念，請推薦下列後續知識點供其挑戰。"
        )

    # 為前 3 個重點概念抓教材片段，控制 prompt 長度
    snippets: List[str] = []
    for fc in focus_concepts[:3]:
        snippets.extend(fetch_concept_snippets(fc["name"], max_snippets=1))

    return {
        "stage": stage,
        "focus_concepts": focus_concepts,
        "guidance_hint": guidance_hint,
        "snippets": [s for s in snippets if s][:5],
    }


# ============================================================
# 5. 學習路徑（取代 learning_analytics.generate_learning_path_recommendations
#               原本只看分數做的硬規則）
# ============================================================
def get_learning_path(target_concept: str) -> Dict[str, Any]:
    """從基礎概念到目標概念的學習路徑。"""
    resolved = resolve_concept_name(target_concept) if target_concept else None
    if not resolved:
        return {"target": target_concept, "paths": []}

    driver = _get_driver()
    if driver is None:
        return {"target": resolved, "paths": []}

    cypher = """
    MATCH (target:Concept {name: $target})
    OPTIONAL MATCH path = (root:Concept)-[:PREREQUISITE_OF*1..5]->(target)
    WITH target, path, length(path) AS depth
    ORDER BY depth ASC
    WITH target, collect({path: [n IN nodes(path) | n.name], depth: depth})[..10] AS paths
    RETURN target.name AS target, target.definition AS def, paths
    """
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            row = s.run(cypher, target=resolved).single()
        if not row:
            return {"target": resolved, "paths": []}
        return dict(row)
    except Exception as e:
        logger.error(f"❌ [GraphRAG] get_learning_path 失敗: {e}")
        return {"target": resolved, "paths": []}


# ============================================================
# 6. 健康檢查
# ============================================================
def healthcheck() -> Dict[str, Any]:
    """供 /api/rag/health 或 debug 用。

    改動：
    * 加 `ok` 欄位，讓 `/api/rag/health` 兩邊 backend 格式對稱
    * 打 upstream `/stats`（存在）而不是 `/health`（不存在，會 404）
    * 帶 `reason` 訊息給前端顯示
    """
    status = {
        "ok": False,
        "neo4j": False,
        "api_server": False,
        "concept_count": 0,
        "reason": "",
    }
    reasons = []

    driver = _get_driver()
    if driver:
        try:
            with driver.session(database=NEO4J_DATABASE) as s:
                row = s.run("MATCH (c:Concept) RETURN count(c) AS n").single()
            status["neo4j"] = True
            status["concept_count"] = (row or {}).get("n", 0)
        except Exception as e:
            logger.warning(f"healthcheck Neo4j failed: {e}")
            reasons.append(f"Neo4j 連線失敗: {e}")
    else:
        reasons.append("Neo4j driver 未初始化")

    try:
        # upstream ComputerScienceKG api_server 沒有 /health，用 /stats 代替
        r = requests.get(f"{GRAPHRAG_API_BASE}/stats", timeout=5)
        status["api_server"] = r.status_code == 200
        if r.status_code != 200:
            reasons.append(f"api_server /stats 回 {r.status_code}")
    except Exception as e:
        reasons.append(f"api_server 無法連線: {e}")

    status["ok"] = status["neo4j"] and status["concept_count"] > 0
    if not status["ok"] and not reasons:
        reasons.append("Neo4j 連得上但沒有 Concept 資料")
    status["reason"] = "OK" if status["ok"] else "; ".join(reasons)
    return status
