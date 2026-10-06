"""GraphRAG 教學引導 proxy。

呼叫 ComputerScienceKG 專案的 api_server.py (port 8001)，
把計算機概論知識圖譜功能暴露給 MIS 前端。

啟動條件：
1. ComputerScienceKG 目錄下執行 `python api_server.py`
2. 確認 http://localhost:8001 可訪問
"""
import os
import time
from flask import Blueprint, jsonify, request, Response, stream_with_context
import requests
from neo4j import GraphDatabase

graphrag_bp = Blueprint("graphrag", __name__, url_prefix="/api/graphrag")

# ============== 設定 ==============
GRAPHRAG_API_BASE = os.getenv("GRAPHRAG_API_BASE", "http://localhost:8001")
# Direct Neo4j inspection is optional.  Its connection values must always be
# supplied by the deployer's local environment; source contains no fallback
# username or password.
NEO4J_URI = os.getenv("NEO4J_URI", "")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

# MIS markdown 教材目錄（用來檢查「在 MIS 教材中查看」按鈕要不要亮）
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MATERIALS_DIR = os.path.join(_BACKEND_DIR, "data", "materials")

_driver = None
_schema_cache = None
_schema_cache_at = 0.0


def _neo4j_configured():
    """Return whether direct Neo4j access was explicitly configured locally."""
    return bool(NEO4J_URI and NEO4J_USER and NEO4J_PASS)


def _get_driver():
    """共用 Neo4j driver；連線失效時自動重建。

    為什麼要這層保護：Neo4j Desktop / 防火牆閒置會把 Bolt 連線斷掉，
    舊 driver 內的 connection pool 就會吐 `defunct connection` 例外。
    這裡用 verify_connectivity() 健康檢查，失敗就丟掉重建。
    """
    global _driver
    if not _neo4j_configured():
        raise RuntimeError(
            "GraphRAG direct Neo4j access is not configured. "
            "Set NEO4J_URI, NEO4J_USERNAME, and NEO4J_PASSWORD locally."
        )
    # 第一次或上次被釋放掉
    if _driver is None:
        _driver = GraphDatabase.driver(
            NEO4J_URI,
            auth=(NEO4J_USER, NEO4J_PASS),
            # keep-alive 防止閒置斷線
            max_connection_lifetime=300,        # 5 分鐘就丟掉重建
            connection_acquisition_timeout=10,  # 10 秒拿不到連線就放棄
            keep_alive=True,
        )
        return _driver

    # 已存在 → 健康檢查；失敗就重建
    try:
        _driver.verify_connectivity()
    except Exception:
        try:
            _driver.close()
        except Exception:
            pass
        _driver = GraphDatabase.driver(
            NEO4J_URI,
            auth=(NEO4J_USER, NEO4J_PASS),
            max_connection_lifetime=300,
            connection_acquisition_timeout=10,
            keep_alive=True,
        )
    return _driver


def _session():
    return _get_driver().session(database=NEO4J_DATABASE)


def _get_graph_schema(force=False):
    """Map supported relationship aliases without querying nonexistent types."""
    global _schema_cache, _schema_cache_at
    if not force and _schema_cache and time.monotonic() - _schema_cache_at < 30:
        return dict(_schema_cache)

    with _session() as session:
        rel_rows = session.run(
            "MATCH ()-[r]->() RETURN DISTINCT type(r) AS type ORDER BY type"
        ).data()
        index_rows = session.run(
            "SHOW INDEXES YIELD name, type, state, labelsOrTypes, properties "
            "RETURN name, type, state, labelsOrTypes, properties ORDER BY name"
        ).data()
        counts = session.run(
            "MATCH (c:Concept) RETURN count(c) AS concepts"
        ).single()

    relationship_types = {row["type"] for row in rel_rows}
    prereq = next(
        (name for name in ("PREREQUISITE_OF", "PREREQ_OF") if name in relationship_types),
        None,
    )
    subtype = next(
        (name for name in ("HAS_SUBTYPE", "IS_A") if name in relationship_types),
        None,
    )
    vector_indexes = [
        row for row in index_rows
        if str(row.get("type", "")).upper() == "VECTOR" and row.get("state") == "ONLINE"
    ]
    warnings = []
    if not prereq:
        warnings.append("找不到先備關係 PREREQUISITE_OF 或 PREREQ_OF")
    if not subtype:
        warnings.append("找不到父子關係 HAS_SUBTYPE 或 IS_A")
    if not vector_indexes:
        warnings.append("找不到 ONLINE 向量索引；目前可檢查圖關係，但無法執行向量 GraphRAG")

    _schema_cache = {
        "database": NEO4J_DATABASE,
        "concept_count": counts["concepts"] if counts else 0,
        "relationship_types": sorted(relationship_types),
        "prereq_relationship": prereq,
        "subtype_relationship": subtype,
        "indexes": index_rows,
        "vector_indexes": vector_indexes,
        "vector_ready": bool(vector_indexes),
        "warnings": warnings,
    }
    _schema_cache_at = time.monotonic()
    return dict(_schema_cache)


def _resolve_concept_name(raw_name):
    """Resolve exact names and aliases before falling back to a substring match."""
    name = (raw_name or "").strip()
    if not name:
        return None
    cypher = """
    MATCH (c:Concept)
    WITH c,
         [a IN coalesce(c.aliases, []) WHERE toLower(a) = toLower($name)] AS exact_aliases,
         [a IN coalesce(c.aliases, []) WHERE toLower(a) CONTAINS toLower($name)] AS partial_aliases
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
    with _session() as session:
        row = session.run(cypher, name=name).single()
    return row["name"] if row else None


@graphrag_bp.route("/schema", methods=["GET"])
def graph_schema_status():
    """Expose the active Neo4j schema and whether vector GraphRAG is ready."""
    return jsonify(_get_graph_schema(force=True))


@graphrag_bp.route("/question-concepts/map", methods=["POST"])
def map_question_concepts():
    """Inspect how one quiz question maps to canonical GraphRAG concepts."""
    data = request.get_json(silent=True) or {}
    question_text = str(data.get("question_text") or "").strip()
    if not question_text:
        return jsonify({"error": "question_text is required"}), 400

    from src.question_concept_mapper import map_question_to_concepts

    mapping = map_question_to_concepts(data)
    return jsonify(mapping)


# ============== Routes ==============
@graphrag_bp.route("/concepts/search", methods=["GET"])
def search_concepts():
    """以名稱搜尋概念，給 autocomplete 用。
    GET /api/graphrag/concepts/search?q=陣列&limit=10
    """
    q = (request.args.get("q") or "").strip()
    limit = int(request.args.get("limit", 20))
    if not q:
        return jsonify({"results": []})

    cypher = """
    MATCH (c:Concept)
    WHERE toLower(c.name) CONTAINS toLower($q)
       OR any(a IN coalesce(c.aliases, []) WHERE toLower(a) CONTAINS toLower($q))
    RETURN c.name AS name, c.category AS category,
           substring(c.definition, 0, 100) AS definition
    ORDER BY size(c.name) ASC
    LIMIT $limit
    """
    with _session() as s:
        rows = s.run(cypher, q=q, limit=limit).data()
    return jsonify({"results": rows})


@graphrag_bp.route("/concepts/categories", methods=["GET"])
def list_categories():
    """列出所有類別與該類別下的概念數，用於首頁分類選單。"""
    cypher = """
    MATCH (c:Concept)
    RETURN c.category AS category, count(*) AS n
    ORDER BY n DESC
    """
    with _session() as s:
        rows = s.run(cypher).data()
    return jsonify({"categories": rows})


@graphrag_bp.route("/concepts/by_category", methods=["GET"])
def concepts_by_category():
    """列某類別下的所有概念，用於展開瀏覽。
    GET /api/graphrag/concepts/by_category?category=資料結構
    """
    cat = request.args.get("category", "")
    cypher = """
    MATCH (c:Concept {category: $cat})
    RETURN c.name AS name,
           substring(c.definition, 0, 120) AS definition,
           c.isFineGrained AS fine
    ORDER BY c.isFineGrained ASC, c.name ASC
    LIMIT 200
    """
    with _session() as s:
        rows = s.run(cypher, cat=cat).data()
    return jsonify({"concepts": rows})


@graphrag_bp.route("/concept/<name>", methods=["GET"])
def get_concept_detail(name):
    """取概念詳情：定義、先輩、後續、子類、父概念、所屬書本。"""
    resolved = _resolve_concept_name(name)
    if not resolved:
        return jsonify({"error": f"concept not found: {name}"}), 404

    schema = _get_graph_schema()
    with _session() as session:
        row = session.run(
            """
            MATCH (c:Concept {name: $name})
            RETURN c.name AS name, c.definition AS definition,
                   c.category AS category, coalesce(c.aliases, []) AS aliases,
                   coalesce(c.bookIds, c.book_ids, []) AS bookIds,
                   coalesce(c.isFineGrained, c.is_fine_grained, false) AS isFineGrained
            """,
            name=resolved,
        ).single()
        prereqs = []
        nexts = []
        subs = []
        parents = []
        if schema["prereq_relationship"]:
            rel = schema["prereq_relationship"]
            prereqs = session.run(
                f"MATCH (p:Concept)-[r:{rel}]->(c:Concept {{name: $name}}) "
                "RETURN p.name AS name, "
                "coalesce(properties(r)['reason'], properties(r)['note'], '') AS reason LIMIT 10",
                name=resolved,
            ).data()
            nexts = session.run(
                f"MATCH (c:Concept {{name: $name}})-[r:{rel}]->(n:Concept) "
                "RETURN n.name AS name, "
                "coalesce(properties(r)['reason'], properties(r)['note'], '') AS reason LIMIT 10",
                name=resolved,
            ).data()
        if schema["subtype_relationship"]:
            rel = schema["subtype_relationship"]
            subs = [item["name"] for item in session.run(
                f"MATCH (c:Concept {{name: $name}})-[:{rel}]->(n:Concept) "
                "RETURN DISTINCT n.name AS name LIMIT 10",
                name=resolved,
            ).data()]
            parents = [item["name"] for item in session.run(
                f"MATCH (p:Concept)-[:{rel}]->(c:Concept {{name: $name}}) "
                "RETURN DISTINCT p.name AS name LIMIT 10",
                name=resolved,
            ).data()]
    if not row:
        return jsonify({"error": "concept not found"}), 404
    result = dict(row)
    result.update({
        "requestedName": name,
        "prereqs": prereqs,
        "nexts": nexts,
        "subs": subs,
        "parents": parents,
        "schema": schema,
    })
    return jsonify(result)


@graphrag_bp.route("/learning_path/<target>", methods=["GET"])
def learning_path(target):
    """計算到達目標概念的學習路徑（從基礎到目標）。"""
    resolved = _resolve_concept_name(target)
    if not resolved:
        return jsonify({"error": f"not found: {target}"}), 404
    schema = _get_graph_schema()
    rel = schema["prereq_relationship"]
    if not rel:
        return jsonify({"target": resolved, "paths": [], "schema": schema})
    cypher = f"""
    MATCH (target:Concept {{name: $target}})
    OPTIONAL MATCH path = (root:Concept)-[:{rel}*1..5]->(target)
    WITH target, path, length(path) AS depth
    ORDER BY depth ASC
    WITH target, collect({{path: [n IN nodes(path) | n.name], depth: depth}})[..20] AS paths
    RETURN target.name AS target, target.definition AS def, paths
    """
    with _session() as s:
        row = s.run(cypher, target=resolved).single()
    if not row:
        return jsonify({"error": "not found"}), 404
    result = dict(row)
    result["schema"] = schema
    return jsonify(result)


@graphrag_bp.route("/concept/<path:name>/paths", methods=["GET"])
def concept_paths(name):
    """Return readable horizontal learning paths for a selected concept.

    Query:
      rel_type=prereq|subtype
      depth=1..6
      limit=1..20 per direction
    """
    rel_type = (request.args.get("rel_type") or "prereq").strip().lower()
    if rel_type not in ("prereq", "subtype"):
        rel_type = "prereq"

    try:
        depth = min(max(int(request.args.get("depth", 4)), 1), 6)
    except (ValueError, TypeError):
        depth = 4
    try:
        limit = min(max(int(request.args.get("limit", 8)), 1), 20)
    except (ValueError, TypeError):
        limit = 8

    resolved = _resolve_concept_name(name)
    if not resolved:
        return jsonify({
            "concept": name,
            "rel_type": rel_type,
            "definition": "",
            "paths": [],
            "error": f"概念「{name}」不存在於 Neo4j",
        }), 404

    schema = _get_graph_schema()
    if rel_type == "subtype":
        rel = schema["subtype_relationship"]
        incoming_label = "父層路徑"
        outgoing_label = "子類路徑"
    else:
        rel = schema["prereq_relationship"]
        incoming_label = "先輩學習路徑"
        outgoing_label = "後續延伸路徑"

    if not rel:
        with _session() as session:
            center = session.run(
                "MATCH (c:Concept {name: $name}) RETURN coalesce(c.definition, '') AS definition",
                name=resolved,
            ).single()
        return jsonify({
            "concept": resolved,
            "requested_concept": name,
            "rel_type": rel_type,
            "definition": center["definition"] if center else "",
            "incoming_label": incoming_label,
            "outgoing_label": outgoing_label,
            "paths": [],
            "schema": schema,
        })

    cypher = f"""
    MATCH (target:Concept {{name: $name}})
    CALL (target) {{
      MATCH p = (root:Concept)-[:{rel}*1..{depth}]->(target)
      WITH p
      ORDER BY length(p) ASC
      LIMIT $limit
      RETURN collect({{
        direction: 'incoming',
        label: $incoming_label,
        depth: length(p),
        nodes: [n IN nodes(p) | {{
          name: n.name,
          category: coalesce(n.category, '其他'),
          definition: coalesce(n.definition, '')
        }}]
      }}) AS incoming_paths
    }}
    CALL (target) {{
      MATCH p = (target)-[:{rel}*1..{depth}]->(leaf:Concept)
      WITH p
      ORDER BY length(p) ASC
      LIMIT $limit
      RETURN collect({{
        direction: 'outgoing',
        label: $outgoing_label,
        depth: length(p),
        nodes: [n IN nodes(p) | {{
          name: n.name,
          category: coalesce(n.category, '其他'),
          definition: coalesce(n.definition, '')
        }}]
      }}) AS outgoing_paths
    }}
    RETURN target.name AS concept,
           coalesce(target.definition, '') AS definition,
           incoming_paths + outgoing_paths AS paths
    """

    with _session() as s:
        row = s.run(
            cypher,
            name=resolved,
            limit=limit,
            incoming_label=incoming_label,
            outgoing_label=outgoing_label,
        ).single()

    if not row:
        return jsonify({
            "concept": name,
            "rel_type": rel_type,
            "definition": "",
            "paths": [],
            "error": f"概念「{name}」不存在於 Neo4j",
        }), 404

    return jsonify({
        "concept": row["concept"],
        "requested_concept": name,
        "rel_type": rel_type,
        "definition": row["definition"],
        "incoming_label": incoming_label,
        "outgoing_label": outgoing_label,
        "paths": row["paths"] or [],
        "schema": schema,
    })


# ============== 新增：以概念為中心的子圖（給知識圖譜視覺化用）==============
@graphrag_bp.route("/concept/<name>/graph", methods=["GET"])
def concept_graph(name):
    """回傳以該概念為中心的子圖，給前端 Cytoscape.js 渲染。

    Query 參數：
        depth     — 先輩 / 後繼鏈擴展層數（預設 2，最大 6）
        max_nodes — 節點上限（預設 60，避免一次塞太多）
        rel_type  — 關係類型篩選（給檢查 GraphRAG 正確性用）
                    • "prereq"  → 只回先輩鏈（PREREQUISITE_OF，含上下游）
                    • "subtype" → 只回父子關係（HAS_SUBTYPE）
                    • "both"    → 全部（預設）

    回傳結構（範例）：
    {
      "center": "虛擬記憶體",
      "nodes": [
        {"id": "虛擬記憶體", "label": "虛擬記憶體", "category": "作業系統",
         "definition": "...", "isCenter": true, "isFineGrained": false},
        {"id": "分頁", "label": "分頁", "category": "作業系統", ...},
        ...
      ],
      "edges": [
        {"source": "分頁", "target": "虛擬記憶體",
         "type": "PREREQUISITE_OF", "reason": "..."},
        {"source": "虛擬記憶體", "target": "需求分頁",
         "type": "HAS_SUBTYPE"},
        ...
      ]
    }
    """
    try:
        # 上限 6：跟 /concept/{name}/paths 對齊；再高圖會爆炸
        depth = min(max(int(request.args.get("depth", 2)), 1), 6)
    except (ValueError, TypeError):
        depth = 2
    try:
        max_nodes = min(max(int(request.args.get("max_nodes", 60)), 10), 200)
    except (ValueError, TypeError):
        max_nodes = 60

    rel_type = (request.args.get("rel_type") or "both").strip().lower()
    if rel_type not in ("prereq", "subtype", "both"):
        rel_type = "both"

    resolved = _resolve_concept_name(name)
    if not resolved:
        return jsonify({
            "center": name,
            "nodes": [], "edges": [],
            "error": f"概念「{name}」不存在於 Neo4j",
        }), 404

    schema = _get_graph_schema()
    prereq_rel = schema["prereq_relationship"]
    subtype_rel = schema["subtype_relationship"]
    per_direction_limit = max(5, max_nodes // 2)
    ancestors = []
    descendants = []
    subs = []
    parents = []

    with _session() as session:
        center_row = session.run(
            "MATCH (c:Concept {name: $name}) RETURN c",
            name=resolved,
        ).single()
        if not center_row:
            return jsonify({
                "center": resolved,
                "nodes": [], "edges": [],
                "error": f"概念「{resolved}」不存在於 Neo4j",
            }), 404
        center = center_row["c"]

        if rel_type in ("prereq", "both") and prereq_rel:
            ancestors = [row["node"] for row in session.run(
                f"MATCH (node:Concept)-[:{prereq_rel}*1..{depth}]->(:Concept {{name: $name}}) "
                "RETURN DISTINCT node LIMIT $limit",
                name=resolved,
                limit=per_direction_limit,
            ).data()]
            descendants = [row["node"] for row in session.run(
                f"MATCH (:Concept {{name: $name}})-[:{prereq_rel}*1..{depth}]->(node:Concept) "
                "RETURN DISTINCT node LIMIT $limit",
                name=resolved,
                limit=per_direction_limit,
            ).data()]

        if rel_type in ("subtype", "both") and subtype_rel:
            subs = [row["node"] for row in session.run(
                f"MATCH (:Concept {{name: $name}})-[:{subtype_rel}*1..{depth}]->(node:Concept) "
                "RETURN DISTINCT node LIMIT $limit",
                name=resolved,
                limit=per_direction_limit,
            ).data()]
            parents = [row["node"] for row in session.run(
                f"MATCH (node:Concept)-[:{subtype_rel}*1..{depth}]->(:Concept {{name: $name}}) "
                "RETURN DISTINCT node LIMIT $limit",
                name=resolved,
                limit=per_direction_limit,
            ).data()]

    # 整理節點
    seen_ids = set()
    nodes = []

    def add_node(n, is_center=False):
        if n is None:
            return
        nid = n.get("name")
        if not nid or nid in seen_ids:
            return
        if not is_center and len(nodes) >= max_nodes:
            return
        seen_ids.add(nid)
        nodes.append({
            "id": nid,
            "label": nid,
            "category": n.get("category", "其他"),
            "definition": (n.get("definition") or "")[:200],
            "isCenter": is_center,
            "isFineGrained": bool(n.get("isFineGrained", n.get("is_fine_grained", False))),
            "bookIds": list(n.get("bookIds") or n.get("book_ids") or []),
        })

    add_node(center, is_center=True)
    for n in ancestors:
        add_node(n)
    for n in descendants:
        add_node(n)
    for n in subs:
        add_node(n)
    for n in parents:
        add_node(n)

    # 補上邊（用另一輪 Cypher，只查 seen_ids 之間的關聯，避免散漫）
    edges = []
    if len(seen_ids) > 1:
        edge_queries = []
        if rel_type in ("prereq", "both") and prereq_rel:
            edge_queries.append(f"""
            MATCH (a:Concept)-[r:{prereq_rel}]->(b:Concept)
            WHERE a.name IN $names AND b.name IN $names
            RETURN a.name AS source, b.name AS target,
                   'PREREQUISITE_OF' AS type,
                   coalesce(properties(r)['reason'], properties(r)['note'], '') AS reason,
                   coalesce(r.confidence, 0.0) AS confidence
            """)
        if rel_type in ("subtype", "both") and subtype_rel:
            edge_queries.append(f"""
            MATCH (a:Concept)-[r:{subtype_rel}]->(b:Concept)
            WHERE a.name IN $names AND b.name IN $names
            RETURN a.name AS source, b.name AS target,
                   'HAS_SUBTYPE' AS type,
                   coalesce(properties(r)['reason'], properties(r)['note'], '是…的子類') AS reason,
                   coalesce(r.confidence, 1.0) AS confidence
            """)
        if edge_queries:
            with _session() as session:
                edges = session.run(
                    " UNION ".join(edge_queries),
                    names=list(seen_ids),
                ).data()

    return jsonify({
        "center": resolved,
        "requested_center": name,
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "depth": depth,
            "rel_type": rel_type,
            "database": schema["database"],
            "prereq_relationship": prereq_rel,
            "subtype_relationship": subtype_rel,
            "vector_ready": schema["vector_ready"],
            "warnings": schema["warnings"],
        },
    })


# ============== 新增：概念在教材中的出處 ==============
@graphrag_bp.route("/concept/<name>/locations", methods=["GET"])
def concept_locations(name):
    """轉發到 ComputerScienceKG api_server 的 /concept/{name}/locations。

    回傳結構（範例）：
    {
      "name": "陣列",
      "total_books": 2,
      "locations": [
        {
          "bookId": "csi_ch09",
          "bookTitle": "Chapter 1 Data Storage",
          "chapterId": "csi_ch09_toc_001",
          "chapterTitle": "Chapter 1 Data Storage",
          "page": 23,
          "chunkSeqId": 3,
          "snippet": "陣列是一種...",
          "hasPdf": true
        }, ...
      ]
    }
    """
    try:
        limit = request.args.get("limit", "30")
        r = requests.get(
            f"{GRAPHRAG_API_BASE}/concept/{name}/locations",
            params={"limit": limit},
            timeout=15,
        )
        r.raise_for_status()
        return jsonify(r.json())
    except requests.exceptions.RequestException as e:
        return jsonify({
            "error": f"無法取得概念出處: {e}",
            "name": name,
            "locations": [],
            "total_books": 0,
        }), 503


# ============== 新增：判斷該概念是否有 MIS markdown 教材 ==============
@graphrag_bp.route("/concept/<name>/material_check", methods=["GET"])
def concept_material_check(name):
    """檢查 backend/data/materials/<name>.md 是否存在；存在就把可跳轉的
    Angular 路徑 (filename) 一起傳回去。"""
    safe_name = name.replace("/", "_").replace("\\", "_")
    candidates = [
        f"{safe_name}.md",
        f"{safe_name}.MD",
    ]
    for fname in candidates:
        fpath = os.path.join(MATERIALS_DIR, fname)
        if os.path.exists(fpath):
            # 前端用：router.navigate(['/dashboard/material-view', filename])
            # filename 不帶 .md，後端會自動補（見 materials_api.py）
            return jsonify({
                "exists": True,
                "filename": safe_name,
            })
    return jsonify({"exists": False, "filename": None})


# ============== 新增：PDF 代理（stream 轉發二進位）==============
@graphrag_bp.route("/pdf/<book_id>", methods=["GET"])
def pdf_proxy(book_id):
    """把 ComputerScienceKG api_server 的 /pdf/{book_id} 內容串流回前端。
    前端用 <iframe src=...> 或 window.open 觸發，瀏覽器會用內建 viewer 顯示。
    """
    try:
        upstream = requests.get(
            f"{GRAPHRAG_API_BASE}/pdf/{book_id}",
            stream=True,
            timeout=30,
        )
        if upstream.status_code != 200:
            return jsonify({
                "error": f"upstream {upstream.status_code}",
                "detail": upstream.text,
            }), upstream.status_code

        def generate():
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk

        # 把上游的 Content-Disposition / Content-Type 都複製過來
        headers = {
            "Content-Type":
                upstream.headers.get("Content-Type", "application/pdf"),
            "Content-Disposition":
                upstream.headers.get("Content-Disposition",
                                     f'inline; filename="{book_id}.pdf"'),
            "Cache-Control": "public, max-age=3600",
        }
        return Response(stream_with_context(generate()), headers=headers)
    except requests.exceptions.RequestException as e:
        return jsonify({
            "error": f"PDF 代理失敗: {e}",
            "hint": "請確認 ComputerScienceKG api_server.py 已啟動 (port 8001)",
        }), 503


@graphrag_bp.route("/ask", methods=["POST"])
def ask_with_graphrag():
    """轉發到 GraphRAG API 的 /query (帶 LLM 答案)。
    request body: {"question": "...", "concept_hint": "陣列"}  # concept_hint 可選
    """
    data = request.get_json() or {}
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    # 如果有 concept_hint，把它拼進問題裡，讓 GraphRAG 檢索更精準
    hint = data.get("concept_hint")
    if hint:
        question = f"關於「{hint}」這個概念：{question}"

    try:
        r = requests.post(
            f"{GRAPHRAG_API_BASE}/query",
            json={"question": question, "top_k": data.get("top_k", 5)},
            timeout=60,
        )
        r.raise_for_status()
        return jsonify(r.json())
    except requests.exceptions.RequestException as e:
        return jsonify({
            "error": f"GraphRAG API 連線失敗: {e}",
            "hint": "請確認 ComputerScienceKG 目錄已啟動 api_server.py"
        }), 503


@graphrag_bp.route("/stats", methods=["GET"])
def stats():
    """整體統計，用於首頁顯示概念總數等資訊。"""
    schema = _get_graph_schema()
    with _session() as session:
        node_counts = session.run(
            "MATCH (c:Concept) WITH count(c) AS concepts "
            "OPTIONAL MATCH (b) WHERE 'Book' IN labels(b) "
            "RETURN concepts, count(b) AS books"
        ).single()
        rel_counts = {
            row["type"]: row["count"]
            for row in session.run(
                "MATCH ()-[r]->() RETURN type(r) AS type, count(r) AS count"
            ).data()
        }
    prereq_rel = schema["prereq_relationship"]
    subtype_rel = schema["subtype_relationship"]
    return jsonify({
        "concepts": node_counts["concepts"] if node_counts else 0,
        "books": node_counts["books"] if node_counts else 0,
        "prereqs": rel_counts.get(prereq_rel, 0),
        "subtypes": rel_counts.get(subtype_rel, 0),
        "database": schema["database"],
        "prereq_relationship": prereq_rel,
        "subtype_relationship": subtype_rel,
        "vector_ready": schema["vector_ready"],
        "warnings": schema["warnings"],
    })
