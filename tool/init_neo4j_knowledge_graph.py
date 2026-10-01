from neo4j import GraphDatabase

def create_courses(tx):
    """
    Creates course nodes in the Neo4j graph.
    """
    courses = [
        "數位邏輯", "作業系統", "資料結構", "電腦網路", "資料庫",
        "AI 與機器學習", "資訊安全", "雲端與虛擬化", "管理資訊系統", "軟體工程與系統開發",
        "數學與統計"
    ]

    for course_name in courses:
        tx.run("CREATE (:Course {name: $name})", name=course_name)
def create_chapters(tx):
    """
    Creates chapter nodes and links them to their respective courses.
    """
    course_chapters = {
        "數位邏輯": [
            "數位邏輯基本概念", "基本邏輯閘", "布林代數與第摩根定理", "布林代數化簡"
        ],
        "作業系統": [
            "作業系統基本概念", "行程管理", "行程同步", "記憶體管理", "儲存管理"
        ],
        "資料結構": [
            "資料結構簡介", "陣列", "鏈結串列", "佇列與堆疊", "樹狀結構"
        ],
        "電腦網路": [
            "概論", "訊號調變與編碼", "區域網路", "區域網路之元件及連線", "網際網路應用"
        ],
        "資料庫": [
            "資料庫概念", "資料庫設計", "SQL Server 使用", "建立資料表"
        ],
        "AI 與機器學習": [
            "使用基礎模型建構 AI 應用導論", "理解基礎模型", "評估方法",
            "評估 AI 系統", "提示工程", "RAG 與代理", "微調", "數據集工程"
        ],
        "資訊安全": [
            "資訊安全認知與風險識別", "信任與安全架構", "數位邊界與防禦部署", "資訊安全管理與未來挑戰"
        ],
        "雲端與虛擬化": [
            "虛擬化技術", "Qemu-KVM", "Libvirt", "Virt-Manager", "網路虛擬化", "傳統存儲技術與 RAID"
        ],
        "管理資訊系統": [
            "組織、管理與連網企業", "資訊科技基礎建設", "數位時代的關鍵系統應用", "建立與維護系統"
        ],
        "軟體工程與系統開發": [
            "軟體工程簡介", "軟體系統需求工程", "系統規格到架構設計", "物件導向軟體工程",
            "系統測試與部署安裝", "軟體系統管理與維護", "軟體系統品質管理",
            "設計模式與軟體重構", "資料庫系統開發", "跨平台可移植性開發"
        ],
        "數學與統計": [
            "數學與統計基礎"
        ]
    }

    for course_name, chapters in course_chapters.items():
        for chapter_name in chapters:
            tx.run("""
                MATCH (c:Course {name: $course_name})
                CREATE (c)-[:HAS_CHAPTER]->(:Chapter {name: $chapter_name})
            """, course_name=course_name, chapter_name=chapter_name)
def create_sections(tx):
    """
    Creates section nodes and links them to their respective chapters.
    """
    chapter_sections = {
        # 數位邏輯
        "數位邏輯基本概念": [
            "數量表示法", "數位系統與類比系統", "邏輯準位與二進位表示法", "數位積體電路與 PLD 簡介"
        ],
        "基本邏輯閘": [
            "基本邏輯關係與布林代數", "或閘、及閘與反閘", "反或閘與反及閘", "互斥或閘與互斥反或閘"
        ],
        "布林代數與第摩根定理": [
            "布林代數特質", "單變數定理", "多變數定理與第摩根定理"
        ],
        "布林代數化簡": [
            "布林代數式簡化法", "卡諾圖與組合邏輯設計步驟"
        ],

        # 作業系統
        "作業系統基本概念": [
            "概說", "作業系統結構"
        ],
        "行程管理": [
            "行程觀念", "執行緒與並行性", "CPU 排班"
        ],
        "行程同步": [
            "同步工具", "同步範例", "死結"
        ],
        "記憶體管理": [
            "主記憶體", "虛擬記憶體"
        ],
        "儲存管理": [
            "大量儲存結構", "輸入/輸出系統"
        ],

        # 資料結構
        "資料結構簡介": [
            "資料結構定義", "資料結構對程式效率影響", "演算法定義", "程式效率分析"
        ],
        "陣列": [
            "一維陣列", "二維陣列"
        ],
        "鏈結串列": [
            "單向鏈結串列", "雙向與環狀鏈結串列"
        ],
        "佇列與堆疊": [
            "佇列", "堆疊"
        ],
        "樹狀結構": [
            "二元樹與二元搜尋樹"
        ],

        # 電腦網路
        "概論": [
            "簡介", "訊號", "訊號傳輸"
        ],
        "訊號調變與編碼": [
            "調變", "類比傳輸與數位傳輸"
        ],
        "區域網路": [
            "區域網路拓樸方式", "區域網路開放架構"
        ],
        "區域網路之元件及連線": [
            "區域網路元件", "區域網路連線實作"
        ],
        "網際網路應用": [
            "TCP/IP 通訊協定"
        ],

        # 資料庫
        "資料庫概念": [
            "資料庫由來", "資料庫管理系統", "資料模型", "三層式架構"
        ],
        "資料庫設計": [
            "設計流程", "個體關係模型", "主鍵與外部鍵", "正規化"
        ],
        "SQL Server 使用": [
            "SQL 語言", "SSMS 操作"
        ],
        "建立資料表": [
            "資料型別", "使用 SQL 敘述新增資料表"
        ],

        # AI 與機器學習
        "使用基礎模型建構 AI 應用導論": [
            "AI 工程崛起", "基礎模型使用案例", "AI 應用規劃"
        ],
        "理解基礎模型": [
            "訓練數據與建模", "後訓練與取樣"
        ],
        "評估方法": [
            "語言建模指標與精確評估"
        ],
        "評估 AI 系統": [
            "模型選擇與設計評估管道"
        ],
        "提示工程": [
            "提示工程最佳實例"
        ],
        "RAG 與代理": [
            "RAG 與代理", "記憶管理"
        ],
        "微調": [
            "微調概述與技術"
        ],
        "數據集工程": [
            "數據調理與增強"
        ],

        # 資訊安全
        "資訊安全認知與風險識別": [
            "資訊安全概論", "資訊法律與事件處理", "資訊安全威脅"
        ],
        "信任與安全架構": [
            "認證、授權與存取控制", "資訊安全架構與設計", "基礎密碼學", "資訊系統與網路模型"
        ],
        "數位邊界與防禦部署": [
            "防火牆與使用政策", "入侵偵測與防禦系統", "惡意程式與防毒", "多層次防禦"
        ],
        "資訊安全管理與未來挑戰": [
            "資訊安全營運與管理", "開發維運安全"
        ],

        # 雲端與虛擬化
        "虛擬化技術": [
            "CPU、伺服器、存儲、網路虛擬化", "Xen、KVM、RHEV 簡介", "VMware / VirtualBox / Hyper-V"
        ],
        "Qemu-KVM": [
            "KVM 原理與架構", "Qemu 架構與運行模式", "Qemu 工具介紹"
        ],
        "Libvirt": [
            "Libvirt 架構與 API", "XML 配置文件"
        ],
        "Virt-Manager": [
            "安裝與使用介紹", "WebVirtMgr 管理平臺"
        ],
        "網路虛擬化": [
            "軟件 Overlay SDN", "硬件 Underlay SDN"
        ],
        "傳統存儲技術與 RAID": [
            "RAID 技術與硬盤接口", "邏輯卷管理"
        ],

        # 管理資訊系統
        "組織、管理與連網企業": [
            "現今全球企業的資訊系統", "全球電子化企業與協同合作", "資訊系統、組織與策略"
        ],
        "資訊科技基礎建設": [
            "資訊科技基礎建設與新興科技", "資料庫與資訊管理", "電傳通訊、網際網路與無線科技", "資訊系統安全"
        ],
        "數位時代的關鍵系統應用": [
            "企業系統應用", "電子商務與數位市場", "知識管理與 AI"
        ],
        "建立與維護系統": [
            "建立資訊系統", "管理專案與全球系統"
        ],

        # 軟體工程與系統開發
        "軟體工程簡介": [
            "軟體工程定義與流程", "軟體系統與開發程序"
        ],
        "軟體系統需求工程": [
            "需求工程與系統模型"
        ],
        "系統規格到架構設計": [
            "軟體系統架構設計"
        ],
        "物件導向軟體工程": [
            "物件導向設計與實務"
        ],
        "系統測試與部署安裝": [
            "系統測試流程"
        ],
        "軟體系統管理與維護": [
            "軟體系統管理", "軟體維護"
        ],
        "軟體系統品質管理": [
            "品質管理原則"
        ],
        "設計模式與軟體重構": [
            "設計模式應用", "軟體重構原則"
        ],
        "資料庫系統開發": [
            "資料庫系統開發流程"
        ],
        "跨平台可移植性開發": [
            "跨平台開發概念"
        ],
        "數學與統計基礎": [
            "集合論", "數列與級數", "極限", "微分", "積分",
            "機率", "統計推論", "常態分配", "假設檢定",
            "線性代數", "數理邏輯", "離散數學"
        ]
    }

    for chapter_name, sections in chapter_sections.items():
        for section_name in sections:
            try:
                tx.run("""
                    MATCH (ch:Chapter {name: $chapter_name})
                    CREATE (ch)-[:HAS_SECTION]->(:Section {name: $section_name})
                """, chapter_name=chapter_name, section_name=section_name)
            except Exception as e:
                print(f"Failed to create section {section_name} for chapter {chapter_name}: {e}")

def create_prerequisite_relationships(tx):
    """
    Creates PREREQUISITE relationships between nodes.
    """
    prerequisite_relations = [
        # 基礎邏輯關係
        ("邏輯準位與二進位表示法", "程式效率分析", "Section", "Section"),
        ("布林代數特質", "多變數定理與第摩根定理", "Section", "Section"),
        ("基本邏輯關係與布林代數", "布林代數化簡", "Section", "Chapter"),

        # 資料結構先修關係
        ("一維陣列", "二維陣列", "Section", "Section"),
        ("陣列", "鏈結串列", "Chapter", "Chapter"),
        ("佇列與堆疊", "樹狀結構", "Chapter", "Chapter"),

        # 作業系統先修關係
        ("作業系統基本概念", "行程管理", "Chapter", "Chapter"),
        ("行程觀念", "執行緒與並行性", "Section", "Section"),
        ("行程管理", "行程同步", "Chapter", "Chapter"),
        ("主記憶體", "虛擬記憶體", "Section", "Section"),

        # 網路基礎先修關係
        ("訊號", "訊號傳輸", "Section", "Section"),
        ("訊號調變與編碼", "區域網路", "Chapter", "Chapter"),
        ("區域網路拓樸方式", "區域網路元件", "Section", "Section"),

        # 資料庫設計先修關係
        ("資料模型", "個體關係模型", "Section", "Section"),
        ("個體關係模型", "主鍵與外部鍵", "Section", "Section"),
        ("主鍵與外部鍵", "正規化", "Section", "Section"),
        ("資料型別", "SQL 語言", "Section", "Section"),

        # 軟體工程先修關係
        ("軟體工程定義與流程", "需求工程與系統模型", "Section", "Section"),
        ("軟體系統架構設計", "物件導向設計與實務", "Section", "Section"),

        # 資訊安全先修關係
        ("資訊安全概論", "認證、授權與存取控制", "Section", "Section"),
        ("基礎密碼學", "防火牆與使用政策", "Section", "Section"),

        # 虛擬化技術先修關係
        ("CPU、伺服器、存儲、網路虛擬化", "KVM 原理與架構", "Section", "Section"),
        ("Qemu 架構與運行模式", "Libvirt 架構與 API", "Section", "Section")
    ]

    for prereq, target, prereq_type, target_type in prerequisite_relations:
        try:
            tx.run(f"""
                MATCH (a:{prereq_type} {{name: $prereq}}), (b:{target_type} {{name: $target}})
                CREATE (b)-[:PREREQUISITE]->(a)
            """, prereq=prereq, target=target)
        except Exception as e:
            print(f"Failed to create prerequisite {target} -> {prereq}: {e}")

def create_similar_relationships(tx):
    """
    Creates SIMILAR_TO relationships between nodes.
    """
    similar_relations = [
        # 邏輯閘相似關係
        ("反或閘與反及閘", "反及閘", "Section"),
        ("互斥或閘與互斥反或閘", "互斥反或閘", "Section"),

        # 資料結構相似關係
        ("佇列", "堆疊", "Section"),
        ("單向鏈結串列", "雙向與環狀鏈結串列", "Section"),

        # 傳輸技術相似關係
        ("類比傳輸與數位傳輸", "數位傳輸", "Section"),

        # 軟體管理相似關係
        ("軟體系統管理", "軟體維護", "Section"),
        ("入侵偵測與防禦系統", "防禦系統", "Section"),

        # 虛擬化技術相似關係
        ("軟件 Overlay SDN", "硬件 Underlay SDN", "Section")
    ]

    for item1, item2, node_type in similar_relations:
        try:
            tx.run(f"""
                MATCH (a:{node_type} {{name: $item1}}), (b:{node_type} {{name: $item2}})
                CREATE (a)-[:SIMILAR_TO]->(b), (b)-[:SIMILAR_TO]->(a)
            """, item1=item1, item2=item2)
        except Exception as e:
            print(f"Failed to create similar relationship {item1} <-> {item2}: {e}")

def create_common_misconception_relationships(tx):
    """
    Creates COMMON_MISCONCEPTION_WITH relationships between nodes.
    """
    misconception_relations = [
        # 邏輯閘混淆
        ("或閘、及閘與反閘", "反或閘與反及閘", "Section"),
        ("互斥或閘與互斥反或閘", "或閘、及閘與反閘", "Section"),

        # 資料結構概念混淆
        ("佇列", "堆疊", "Section"),
        ("二元樹與二元搜尋樹", "樹狀結構", "Section"),
        ("單向鏈結串列", "雙向與環狀鏈結串列", "Section"),
        ("主鍵與外部鍵", "外部鍵", "Section"),

        # 記憶體管理混淆
        ("主記憶體", "虛擬記憶體", "Section"),
        ("記憶體管理", "儲存管理", "Chapter"),

        # 行程概念混淆
        ("行程觀念", "執行緒與並行性", "Section"),

        # 網路傳輸混淆
        ("調變", "類比傳輸與數位傳輸", "Section"),

        # 軟體開發階段混淆
        ("軟體系統管理", "軟體維護", "Section"),
        ("系統測試流程", "品質管理原則", "Section")
    ]

    for item1, item2, node_type in misconception_relations:
        try:
            tx.run(f"""
                MATCH (a:{node_type} {{name: $item1}}), (b:{node_type} {{name: $item2}})
                CREATE (a)-[:COMMON_MISCONCEPTION_WITH]->(b)
            """, item1=item1, item2=item2)
        except Exception as e:
            print(f"Failed to create misconception relationship {item1} -> {item2}: {e}")

def create_cross_domain_relationships(tx):
    """
    Creates CROSS_DOMAIN_LINK relationships between nodes.
    """
    cross_domain_relations = [
        # 數位邏輯跨領域關聯
        ("邏輯準位與二進位表示法", "多層次防禦", "Section", "Section"),
        ("數位積體電路與 PLD 簡介", "CPU、伺服器、存儲、網路虛擬化", "Section", "Section"),

        # 作業系統跨領域關聯
        ("行程管理", "軟體系統架構設計", "Chapter", "Section"),
        ("記憶體管理", "程式效率分析", "Chapter", "Section"),
        ("儲存管理", "資料型別", "Chapter", "Section"),
        ("CPU 排班", "RAID 技術與硬盤接口", "Section", "Section"),
        ("虛擬記憶體", "Libvirt 架構與 API", "Section", "Section"),
        ("輸入/輸出系統", "TCP/IP 通訊協定", "Section", "Section"),

        # 資料結構跨領域關聯
        ("演算法定義", "微調概述與技術", "Section", "Section"),
        ("樹狀結構", "SQL 語言", "Chapter", "Section"),
        ("佇列", "同步工具", "Section", "Section"),

        # 電腦網路跨領域關聯
        ("TCP/IP 通訊協定", "資訊安全架構與設計", "Section", "Section"),
        ("區域網路", "現今全球企業的資訊系統", "Chapter", "Section"),
        ("訊號傳輸", "網路虛擬化", "Section", "Chapter"),
        ("網際網路應用", "軟體系統架構設計", "Section", "Section"),

        # 資料庫跨領域關聯
        ("正規化", "軟體系統架構設計", "Section", "Section"),
        ("設計流程", "需求工程與系統模型", "Section", "Section"),
        ("數據調理與增強", "資料型別", "Section", "Section"),
        ("企業系統應用", "資料庫管理系統", "Section", "Section"),

        # AI與機器學習跨領域關聯
        ("理解基礎模型", "資料庫管理系統", "Chapter", "Section"),
        ("提示工程最佳實例", "需求工程與系統模型", "Section", "Section"),
        ("RAG 與代理", "二元樹與二元搜尋樹", "Section", "Section"),
        ("微調概述與技術", "品質管理原則", "Section", "Section"),
        ("數據調理與增強", "正規化", "Section", "Section"),
        ("語言建模指標與精確評估", "入侵偵測與防禦系統", "Section", "Section"),

        # 資訊安全跨領域關聯
        ("基礎密碼學", "TCP/IP 通訊協定", "Section", "Section"),
        ("認證、授權與存取控制", "大量儲存結構", "Section", "Section"),
        ("防火牆與使用政策", "區域網路元件", "Section", "Section"),
        ("入侵偵測與防禦系統", "語言建模指標與精確評估", "Section", "Section"),

        # 雲端與虛擬化跨領域關聯
        ("虛擬化技術", "作業系統結構", "Chapter", "Section"),
        ("軟件 Overlay SDN", "區域網路開放架構", "Section", "Section"),
        ("KVM 原理與架構", "主記憶體", "Section", "Section"),

        # 管理資訊系統跨領域關聯
        ("企業系統應用", "資料庫管理系統", "Section", "Section"),
        ("電子商務與數位市場", "資訊安全架構與設計", "Section", "Section"),
        ("知識管理與 AI", "AI 工程崛起", "Section", "Section"),
        ("建立資訊系統", "軟體工程定義與流程", "Section", "Section"),

        # 軟體工程跨領域關聯
        ("軟體工程定義與流程", "建立資訊系統", "Section", "Section"),
        ("需求工程與系統模型", "提示工程最佳實例", "Section", "Section"),
        ("軟體系統架構設計", "行程管理", "Section", "Chapter"),
        ("系統測試流程", "模型選擇與設計評估管道", "Section", "Section"),
        ("跨平台開發概念", "軟件 Overlay SDN", "Section", "Section")
    ]

    for item1, item2, type1, type2 in cross_domain_relations:
        try:
            tx.run(f"""
                MATCH (a:{type1} {{name: $item1}}), (b:{type2} {{name: $item2}})
                CREATE (a)-[:CROSS_DOMAIN_LINK]->(b), (b)-[:CROSS_DOMAIN_LINK]->(a)
            """, item1=item1, item2=item2)
        except Exception as e:
            print(f"Failed to create cross-domain link {item1} <-> {item2}: {e}")

def init_neo4j_knowledge_graph():
    """
    Initializes the Neo4j knowledge graph by creating nodes and relationships.
    """
    # 從config獲取Neo4j連接配置
    import sys
    import os

    # 添加backend目錄到Python路徑
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, backend_dir)

    # 切換工作目錄到backend，確保相對路徑正確
    os.chdir(backend_dir)

    from config import Config
    uri = Config.NEO4J_URI
    username = Config.NEO4J_USERNAME
    password = Config.NEO4J_PASSWORD

    driver = GraphDatabase.driver(uri, auth=(username, password))

    with driver.session() as session:

        # 清除舊的資料，以便重新建立
        session.run("MATCH (n) DETACH DELETE n")


        session.execute_write(create_courses)

        session.execute_write(create_chapters)

        session.execute_write(create_sections)

        session.execute_write(create_prerequisite_relationships)

        session.execute_write(create_similar_relationships)

        session.execute_write(create_common_misconception_relationships)

        session.execute_write(create_cross_domain_relationships)

    driver.close()


# 移除 if __name__ == '__main__' 避免重複執行
# if __name__ == '__main__':
#     init_neo4j_knowledge_graph()