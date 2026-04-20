# 本体建模工具（简化版）

这是一个最小可运行的本体可视化建模系统，目标是模拟 Protege 的“图形化建模体验”：

- 在画布中新增本体方块
- 选中父本体后新增子本体（自动生成 `is-a` 父子连线）
- 在任意两个本体之间拉线并命名关系
- 拖动本体位置并保存
- 本地 OCR 文档识别（图片/txt/csv）并勾选导入本体

## 技术栈

- 前端：Vue 3 + Vite
- 后端：Python + FastAPI
- 数据存储：后端本地 JSON 文件（`backend/data.json`，运行时自动生成）

## 目录结构

```text
.
├─ backend/
│  ├─ app/main.py
│  └─ requirements.txt
└─ frontend/
   ├─ src/App.vue
   └─ package.json
```

## 1. 启动后端

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app\main.py
```

## 2. 启动前端

另开一个终端：

```bash
cd frontend
npm install
npm run dev
```

浏览器访问 `http://127.0.0.1:5173`

## 3. 使用说明

- 点击“新增本体”：创建一个本体节点
- 选中某个本体后点击“新增子本体”：创建父子关系
- 选中某个本体后点击“关系连线”：再点击目标本体并输入关系名
- 拖拽节点：自动保存到后端
- OCR 导入：
  - 侧边栏选择文件（支持 `.png/.jpg/.jpeg/.bmp/.webp/.txt/.csv`）
  - 点击“开始识别”获取候选词条
  - 勾选需要的词条后点击“一键导入勾选项”
  - 导入后可直接开启连线模式建立关系

## 4. OCR 说明

- OCR 模型：`rapidocr_onnxruntime`（本地离线运行）
- 中文名词抽取：`jieba`
- 表格识别策略：基于 OCR 框的行列密度判断（可区分“表格”与“句子”）

## 可继续扩展

- 节点编辑/删除、关系删除
- 缩放与小地图
- OWL / RDF 导入导出
- 多用户协同
