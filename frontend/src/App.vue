<script setup>
import { computed, onMounted, onUnmounted, ref, nextTick } from "vue";

// Use relative path for same-origin or explicit API_BASE. Fallback to current hostname with 8000 port.
const defaultApiBase = typeof window !== 'undefined' ? `http://${window.location.hostname}:8000` : "http://127.0.0.1:8000";
const API_BASE = import.meta.env.VITE_API_BASE || defaultApiBase;

const nodes = ref([]);
const edges = ref([]);
const inferenceRules = ref([]);
const mutexRules = ref([]);
const selectedNodeId = ref(null);
const selectedEdgeId = ref(null);
const connectMode = ref(false);
const connectSourceId = ref(null);
const statusMessage = ref("就绪");

const draggingNodeId = ref(null);
const dragOffset = ref({ x: 0, y: 0 });
const canvasRef = ref(null);

// 画布平移与缩放状态
const viewOffset = ref({ x: 0, y: 0 });
const zoom = ref(1);
const isPanning = ref(false);
const panStart = ref({ x: 0, y: 0 });

function onWheel(event) {
  if (!canvasRef.value) return;
  
  // 缩放灵敏度
  const zoomSensitivity = 0.001;
  const delta = -event.deltaY * zoomSensitivity;
  let newZoom = zoom.value * (1 + delta);
  
  // 限制缩放范围 (10% 到 500%)
  newZoom = Math.max(0.1, Math.min(newZoom, 5));

  // 以鼠标位置为中心进行缩放
  const rect = canvasRef.value.getBoundingClientRect();
  const mouseX = event.clientX - rect.left;
  const mouseY = event.clientY - rect.top;

  const scaleRatio = newZoom / zoom.value;
  
  viewOffset.value = {
    x: mouseX - (mouseX - viewOffset.value.x) * scaleRatio,
    y: mouseY - (mouseY - viewOffset.value.y) * scaleRatio
  };
  
  zoom.value = newZoom;
}

// 自定义 Prompt 状态
const promptState = ref({
  visible: false,
  title: "",
  value: "",
  defaultValue: "",
  placeholder: "",
  resolve: null,
  reject: null
});
const promptInputRef = ref(null);

// 自定义 Confirm 状态
const confirmState = ref({
  visible: false,
  title: "",
  message: "",
  resolve: null
});

// OCR 导入状态
const ocrFile = ref(null);
const ocrLoading = ref(false);
const ocrResult = ref(null);
const ocrCandidates = ref([]);
const ocrDialogVisible = ref(false);
const ocrPreviewUrl = ref("");
const ocrPreviewText = ref("");
const ocrPreviewType = ref("");

// 已有节点重挂载（转为子本体）状态
const reparentMode = ref(false);
const reparentSourceId = ref(null);

// 右键菜单状态
const contextMenu = ref({
  visible: false,
  x: 0,
  y: 0,
  type: 'canvas', // 'canvas' 或 'node'
  nodeId: null
});

// 节点折叠状态 (记录被折叠的节点 ID)
const collapsedNodeIds = ref(new Set());

// ========================
// 弹窗逻辑
// ========================
async function customPrompt(title, defaultValue = "", placeholder = "请输入") {
  return new Promise((resolve, reject) => {
    promptState.value = {
      visible: true,
      title,
      value: defaultValue,
      defaultValue,
      placeholder,
      resolve,
      reject
    };
    nextTick(() => {
      if (promptInputRef.value) {
        promptInputRef.value.focus();
        promptInputRef.value.select();
      }
    });
  });
}

async function customConfirm(title, message) {
  return new Promise((resolve) => {
    confirmState.value = {
      visible: true,
      title,
      message,
      resolve
    };
  });
}

function handleConfirm(result) {
  if (confirmState.value.resolve) {
    confirmState.value.resolve(result);
  }
  confirmState.value.visible = false;
}

function submitPrompt() {
  if (!promptState.value.visible) return;
  const val = promptState.value.value.trim();
  if (promptState.value.resolve) {
    promptState.value.resolve(val || null);
  }
  closePrompt();
}

function cancelPrompt() {
  if (!promptState.value.visible) return;
  if (promptState.value.resolve) {
    promptState.value.resolve(null);
  }
  closePrompt();
}

function closePrompt() {
  promptState.value.visible = false;
  promptState.value.resolve = null;
  promptState.value.reject = null;
}

function setOcrFile(file) {
  ocrFile.value = file;
  ocrResult.value = null;
  ocrCandidates.value = [];
  ocrDialogVisible.value = false;

  if (ocrPreviewUrl.value) {
    URL.revokeObjectURL(ocrPreviewUrl.value);
    ocrPreviewUrl.value = "";
  }
  ocrPreviewText.value = "";
  ocrPreviewType.value = "";

  if (file) {
    if (file.type.startsWith("image/")) {
      ocrPreviewType.value = "image";
      ocrPreviewUrl.value = URL.createObjectURL(file);
    } else {
      ocrPreviewType.value = "text";
      const reader = new FileReader();
      reader.onload = (e) => {
        ocrPreviewText.value = e.target.result;
      };
      reader.readAsText(file);
    }
  }
}

function onOcrFileChange(event) {
  const file = event.target.files?.[0] || null;
  setOcrFile(file);
}

async function useSampleOcrFile() {
  try {
    const response = await fetch(`${API_BASE}/api/ocr/sample-file`);
    if (!response.ok) {
      throw new Error(await response.text());
    }
    const blob = await response.blob();
    const sampleFile = new File([blob], "test.png", { type: blob.type || "image/png" });
    setOcrFile(sampleFile);
    statusMessage.value = "已加载示例文件 test.png，可直接开始识别";
  } catch (error) {
    statusMessage.value = `加载示例文件失败：${error.message}`;
  }
}

function toggleAllOcrCandidates(checked) {
  ocrCandidates.value = ocrCandidates.value.map((item) => ({ ...item, selected: checked }));
}

const selectedOcrEntities = computed(() =>
  ocrCandidates.value.filter((item) => item.selected).map((item) => item.text)
);

// 规则管理
const rulesDialogVisible = ref(false);
const newMutex = ref({ rel1: '', rel2: '' });
const newInference = ref({ rel1: '', rel2: '', inferred_rel: '' });
const uniqueRelations = computed(() => Array.from(new Set(edges.value.map(e => e.relation))).filter(Boolean));

function addMutexRule() {
  if (!newMutex.value.rel1 || !newMutex.value.rel2) {
    statusMessage.value = "请填写完整的互斥关系";
    return;
  }
  mutexRules.value.push({
    id: Date.now().toString(),
    rel1: newMutex.value.rel1,
    rel2: newMutex.value.rel2
  });
  newMutex.value = { rel1: '', rel2: '' };
  syncGraph();
}

function removeMutexRule(id) {
  mutexRules.value = mutexRules.value.filter(r => r.id !== id);
  syncGraph();
}

function addInferenceRule() {
  if (!newInference.value.rel1 || !newInference.value.rel2 || !newInference.value.inferred_rel) {
    statusMessage.value = "请填写完整的推理关系";
    return;
  }
  inferenceRules.value.push({
    id: Date.now().toString(),
    rel1: newInference.value.rel1,
    rel2: newInference.value.rel2,
    inferred_rel: newInference.value.inferred_rel
  });
  newInference.value = { rel1: '', rel2: '', inferred_rel: '' };
  syncGraph();
}

function removeInferenceRule(id) {
  inferenceRules.value = inferenceRules.value.filter(r => r.id !== id);
  syncGraph();
}

// ========================
// 核心逻辑 & 节点展示
// ========================
const selectedNode = computed(() =>
  nodes.value.find((node) => node.id === selectedNodeId.value)
);

const selectedEdge = computed(() =>
  edges.value.find((edge) => edge.id === selectedEdgeId.value)
);

// 删除逻辑
async function deleteSelectedNode() {
  if (!selectedNode.value) return;
  const confirmed = await customConfirm("删除本体", `确定要删除本体 "${selectedNode.value.name}" 及其所有关联关系吗？`);
  if (!confirmed) return;
  
  const id = selectedNode.value.id;
  nodes.value = nodes.value.filter(n => n.id !== id);
  edges.value = edges.value.filter(e => e.source !== id && e.target !== id);
  selectedNodeId.value = null;
  await syncGraph();
  statusMessage.value = "已删除本体";
}

async function deleteSelectedEdge() {
  if (!selectedEdge.value) return;
  const confirmed = await customConfirm("删除关系", `确定要删除该关系吗？`);
  if (!confirmed) return;

  const id = selectedEdge.value.id;
  edges.value = edges.value.filter(e => e.id !== id);
  selectedEdgeId.value = null;
  await syncGraph();
  statusMessage.value = "已删除关系";
}

// 属性编辑逻辑
function addAttribute() {
  if (!selectedNode.value) return;
  if (!selectedNode.value.attributes) {
    selectedNode.value.attributes = [];
  }
  selectedNode.value.attributes.push({ key: "新属性", value: "" });
  syncGraph();
}

function removeAttribute(idx) {
  if (!selectedNode.value) return;
  selectedNode.value.attributes.splice(idx, 1);
  syncGraph();
}

function updateAttribute() {
  syncGraph();
}

// 关系特征编辑逻辑
function toggleCharacteristic(char) {
  if (!selectedEdge.value) return;
  if (!selectedEdge.value.characteristics) {
    selectedEdge.value.characteristics = [];
  }
  const idx = selectedEdge.value.characteristics.indexOf(char);
  if (idx > -1) {
    selectedEdge.value.characteristics.splice(idx, 1);
  } else {
    selectedEdge.value.characteristics.push(char);
  }
  syncGraph();
}

// 递归查找被折叠节点的所有子孙节点
const hiddenNodeIds = computed(() => {
  const hidden = new Set();
  const hideChildren = (parentId) => {
    nodes.value.forEach(n => {
      if (n.parent_id === parentId) {
        hidden.add(n.id);
        hideChildren(n.id);
      }
    });
  };
  collapsedNodeIds.value.forEach(id => hideChildren(id));
  return hidden;
});

// 过滤掉被隐藏的节点和连线
const visibleNodes = computed(() => 
  nodes.value.filter(n => !hiddenNodeIds.value.has(n.id))
);

const visibleEdges = computed(() => 
  edges.value.filter(e => !hiddenNodeIds.value.has(e.source) && !hiddenNodeIds.value.has(e.target))
);

const nodeMap = computed(() => {
  const map = new Map();
  visibleNodes.value.forEach((node) => map.set(node.id, node));
  return map;
});

const edgeViews = computed(() => {
  return visibleEdges.value
    .map((edge) => {
      const source = nodeMap.value.get(edge.source);
      const target = nodeMap.value.get(edge.target);
      if (!source || !target) {
        return null;
      }

      // 计算曲线/直线路径
      const x1 = source.x + 80; // 160px width / 2
      const y1 = source.y + 35; // approx height / 2
      const x2 = target.x + 80;
      const y2 = target.y + 35;
      
      let path = "";
      if (edge.kind === "parent-child") {
        // 树状正交折线 (Orthogonal Line)
        // 比如从父节点底部中心出来，走一段垂直，然后再水平拐向子节点顶部中心
        const sx = source.x + 80; // 父底部
        const sy = source.y + 70;
        const tx = target.x + 80; // 子顶部
        const ty = target.y;
        
        // 我们向下延伸一点，再转折
        const midY = sy + (ty - sy) / 2;
        path = `M ${sx} ${sy} L ${sx} ${midY} L ${tx} ${midY} L ${tx} ${ty}`;
        
        // 覆盖原始 x, y 为新的连接点，以便箭头和文字计算
        return { ...edge, x1: sx, y1: sy, x2: tx, y2: ty, path };
      } else {
        // 普通关系线为两点直连
        path = `M ${x1} ${y1} L ${x2} ${y2}`;
        return { ...edge, x1, y1, x2, y2, path };
      }
    })
    .filter(Boolean);
});

// 辅助方法：检查节点是否有子节点
function hasChildren(nodeId) {
  return nodes.value.some(n => n.parent_id === nodeId);
}

// 展开/折叠切换
function toggleCollapse(nodeId) {
  const newSet = new Set(collapsedNodeIds.value);
  if (newSet.has(nodeId)) {
    newSet.delete(nodeId);
  } else {
    newSet.add(nodeId);
  }
  collapsedNodeIds.value = newSet;
  hideContextMenu();
}

// ========================
// 右键菜单逻辑
// ========================
function showContextMenu(event, type, nodeId = null) {
  if (!canvasRef.value) return;
  const rect = canvasRef.value.getBoundingClientRect();
  contextMenu.value = {
    visible: true,
    x: event.clientX - rect.left,
    y: event.clientY - rect.top,
    type,
    nodeId
  };
}

function hideContextMenu() {
  contextMenu.value.visible = false;
}

// 从菜单开始连线
function startConnectionFromMenu(nodeId) {
  hideContextMenu();
  selectedNodeId.value = nodeId;
  enableConnectMode();
}

async function request(path, options = {}) {
  const isFormData = options.body instanceof FormData;
  const headers = isFormData
    ? { ...(options.headers || {}) }
    : { "Content-Type": "application/json", ...(options.headers || {}) };
  const response = await fetch(`${API_BASE}${path}`, {
    headers,
    ...options
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

async function loadGraph() {
  try {
    const graph = await request("/api/graph");
    nodes.value = graph.nodes || [];
    edges.value = graph.edges || [];
    inferenceRules.value = graph.inference_rules || [];
    mutexRules.value = graph.mutex_rules || [];
    statusMessage.value = "已加载后端数据";
  } catch (error) {
    statusMessage.value = `加载失败：${error.message}`;
  }
}

async function syncGraph() {
  try {
    await request("/api/graph", {
      method: "PUT",
      body: JSON.stringify({
        nodes: nodes.value,
        edges: edges.value,
        inference_rules: inferenceRules.value,
        mutex_rules: mutexRules.value
      })
    });
  } catch (error) {
    statusMessage.value = `同步失败：${error.message}`;
  }
}

function canUseLlmOcr() {
  if (!ocrFile.value) return false;
  const type = ocrFile.value.type || "";
  const name = (ocrFile.value.name || "").toLowerCase();
  return type.startsWith("image/") || [".png", ".jpg", ".jpeg", ".bmp", ".webp"].some((ext) => name.endsWith(ext));
}

async function runOcrExtract(engine = "local") {
  if (!ocrFile.value) {
    statusMessage.value = "请先选择文件";
    return;
  }
  if (engine === "llm" && !canUseLlmOcr()) {
    statusMessage.value = "大模型 OCR 目前仅支持图片文件";
    return;
  }
  try {
    ocrLoading.value = true;
    const formData = new FormData();
    formData.append("file", ocrFile.value);
    const result = await request(`/api/ocr/extract?engine=${engine}`, {
      method: "POST",
      body: formData
    });
    ocrResult.value = result;
    ocrCandidates.value = (result.entities || []).map((text) => ({
      text,
      selected: true
    }));
    ocrDialogVisible.value = true;
    statusMessage.value = `${engine === "llm" ? "大模型 OCR" : "本地 OCR"} 识别完成：${ocrCandidates.value.length} 项`;
  } catch (error) {
    statusMessage.value = `${engine === "llm" ? "大模型 OCR" : "本地 OCR"} 识别失败：${error.message}`;
  } finally {
    ocrLoading.value = false;
  }
}

async function importOcrToCanvas() {
  const entities = selectedOcrEntities.value;
  if (!entities.length) {
    statusMessage.value = "请至少勾选一项再导入";
    return;
  }
  try {
    const payload = {
      entities,
      start_x: 120 - viewOffset.value.x,
      start_y: 120 - viewOffset.value.y,
      spacing_x: 180,
      spacing_y: 120
    };
    const result = await request("/api/ocr/import", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    await loadGraph();
    ocrDialogVisible.value = false;
    statusMessage.value = `已导入 ${result.created_count} 项，跳过 ${result.skipped_entities.length} 项`;
  } catch (error) {
    statusMessage.value = `导入失败：${error.message}`;
  }
}

async function createNode(payload) {
  const node = await request("/api/nodes", {
    method: "POST",
    body: JSON.stringify(payload)
  });
  // 必须重新加载图，因为后端会同步创建父子关系连线
  await loadGraph();
  return node;
}

async function createEdge(payload) {
  const edge = await request("/api/edges", {
    method: "POST",
    body: JSON.stringify(payload)
  });
  // 同样重新加载以保持同步
  await loadGraph();
  return edge;
}

async function reparentNode(nodeId, newParentId) {
  return request(`/api/nodes/${nodeId}/reparent`, {
    method: "POST",
    body: JSON.stringify({ new_parent_id: newParentId })
  });
}

async function clearGraph() {
  const confirmed = await customConfirm("清空画布", "确定要清空当前画布上的所有本体和关系吗？此操作不可撤销。");
  if (!confirmed) return;
  try {
    nodes.value = [];
    edges.value = [];
    await syncGraph();
    statusMessage.value = "画布已清空";
    selectedNodeId.value = null;
    collapsedNodeIds.value = new Set();
  } catch (error) {
    statusMessage.value = `清空失败：${error.message}`;
  }
}

async function addOntology(x = null, y = null) {
  const name = await customPrompt("请输入本体名称", "", "如：Person");
  if (!name) return;

  try {
    const node = await createNode({
      name,
      x: x !== null ? x : (120 + Math.random() * 220 - viewOffset.value.x) / zoom.value,
      y: y !== null ? y : (120 + Math.random() * 220 - viewOffset.value.y) / zoom.value
    });
    selectedNodeId.value = node.id;
    statusMessage.value = "已新增本体";
  } catch (error) {
    statusMessage.value = `新增失败：${error.message}`;
  }
}

async function addRootOntologyMenu() {
  const { x, y } = contextMenu.value;
  hideContextMenu();
  // 修正侧边栏宽度带来的偏移，并考虑画布平移与缩放
  await addOntology((x - viewOffset.value.x) / zoom.value, (y - viewOffset.value.y) / zoom.value);
}

async function addChildOntology(parentId = null) {
  const targetParentId = parentId || selectedNodeId.value;
  if (!targetParentId) {
    statusMessage.value = "请先选中一个父本体";
    return;
  }
  hideContextMenu();

  const parentNode = nodes.value.find(n => n.id === targetParentId);
  if (!parentNode) return;

  const name = await customPrompt("请输入子本体名称", "", "如：Student");
  if (!name) return;

  // 树状布局计算 (水平铺开)
  const siblings = nodes.value.filter(n => n.parent_id === targetParentId);
  const xOffset = siblings.length * 180 - (siblings.length > 0 ? 90 : 0);

  try {
    await createNode({
      name,
      x: parentNode.x + xOffset,
      y: parentNode.y + 130, // 自动放置在父节点正下方
      parent_id: targetParentId
    });
    
    // 如果父节点被折叠，自动展开
    if (collapsedNodeIds.value.has(targetParentId)) {
      toggleCollapse(targetParentId);
    }
    statusMessage.value = "已新增子本体";
  } catch (error) {
    statusMessage.value = `新增失败：${error.message}`;
  }
}

function enableConnectMode() {
  if (!selectedNodeId.value) {
    statusMessage.value = "请先选中一个起始本体";
    return;
  }
  connectMode.value = true;
  connectSourceId.value = selectedNodeId.value;
  reparentMode.value = false;
  reparentSourceId.value = null;
  statusMessage.value = "连线模式：请点击目标本体";
}

function cancelConnectMode() {
  connectMode.value = false;
  connectSourceId.value = null;
}

function enableReparentMode(nodeId = null) {
  const sourceId = nodeId || selectedNodeId.value;
  if (!sourceId) {
    statusMessage.value = "请先选择要移动的本体";
    return;
  }
  reparentMode.value = true;
  reparentSourceId.value = sourceId;
  connectMode.value = false;
  connectSourceId.value = null;
  statusMessage.value = "子本体模式：请点击目标父本体";
}

function cancelReparentMode() {
  reparentMode.value = false;
  reparentSourceId.value = null;
}

function startReparentFromMenu(nodeId) {
  hideContextMenu();
  selectedNodeId.value = nodeId;
  enableReparentMode(nodeId);
}

async function onClickNode(nodeId) {
  selectedNodeId.value = nodeId;
  selectedEdgeId.value = null;
  if (reparentMode.value) {
    if (!reparentSourceId.value) {
      reparentSourceId.value = nodeId;
      statusMessage.value = "已设置待移动本体，请选择目标父本体";
      return;
    }
    if (reparentSourceId.value === nodeId) {
      statusMessage.value = "目标父本体不能与当前本体相同";
      return;
    }
    try {
      await reparentNode(reparentSourceId.value, nodeId);
      await loadGraph();
      statusMessage.value = "已更新父子关系";
    } catch (error) {
      statusMessage.value = `更新父子关系失败：${error.message}`;
    } finally {
      cancelReparentMode();
    }
    return;
  }

  if (!connectMode.value) return;

  if (!connectSourceId.value) {
    connectSourceId.value = nodeId;
    statusMessage.value = "已设置起始本体，请选择目标本体";
    return;
  }

  if (connectSourceId.value === nodeId) {
    statusMessage.value = "起始和目标不能相同";
    return;
  }

  const relation = await customPrompt("请输入关系名称", "关联", "例如：关联、依赖、包含");
  if (!relation) return;

  try {
    await createEdge({
      source: connectSourceId.value,
      target: nodeId,
      relation,
      kind: "relation"
    });
    await loadGraph(); // 重新加载以获取可能通过规则自动推导出的新边
    statusMessage.value = "已创建本体关系。请继续选择下一个起始本体，或取消连线模式。";
  } catch (error) {
    statusMessage.value = `创建关系失败：${error.message}`;
  } finally {
    // 保持连线模式开启，只清空起始节点，以便连续连线
    connectSourceId.value = null;
  }
}

function startDrag(event, node) {
  draggingNodeId.value = node.id;
  dragOffset.value = {
    x: (event.clientX - viewOffset.value.x) / zoom.value - node.x,
    y: (event.clientY - viewOffset.value.y) / zoom.value - node.y
  };
}

function startPanning(event) {
  if (event.button !== 0) return; // 仅左键平移
  // 弹窗或右键菜单打开时，不启动平移
  if (promptState.value.visible || confirmState.value.visible || ocrDialogVisible.value || contextMenu.value.visible || rulesDialogVisible.value) return;
  isPanning.value = true;
  panStart.value = {
    x: event.clientX - viewOffset.value.x,
    y: event.clientY - viewOffset.value.y
  };
}

async function onPointerMove(event) {
  if (draggingNodeId.value) {
    const node = nodes.value.find((item) => item.id === draggingNodeId.value);
    if (!node) return;
    node.x = (event.clientX - viewOffset.value.x) / zoom.value - dragOffset.value.x;
    node.y = (event.clientY - viewOffset.value.y) / zoom.value - dragOffset.value.y;
  } else if (isPanning.value) {
    viewOffset.value = {
      x: event.clientX - panStart.value.x,
      y: event.clientY - panStart.value.y
    };
  }
}

async function onPointerUp() {
  if (draggingNodeId.value) {
    draggingNodeId.value = null;
    try {
      await syncGraph();
      statusMessage.value = "位置已保存";
    } catch (error) {
      statusMessage.value = `保存位置失败：${error.message}`;
    }
  }
  isPanning.value = false;
}

// ========================
// 符号格式化逻辑
// ========================
function formatSymbol(text) {
  if (!text) return "";

  // 仅对高置信度的物理量符号做格式化，避免把普通英文词错误渲染为上下标
  // 1) 下划线模式：Q_p, TDS_in, p_var
  const underscoreMatch = text.match(/^([A-Za-z]{1,5})_([A-Za-z0-9]{1,8})$/);
  if (underscoreMatch) {
    return `<i>${underscoreMatch[1]}</i><sub>${underscoreMatch[2]}</sub>`;
  }

  // 2) 数字下标模式：CO2, O3
  const numericSubMatch = text.match(/^([A-Za-z]{1,5})(\d{1,3})$/);
  if (numericSubMatch) {
    return `${numericSubMatch[1]}<sub>${numericSubMatch[2]}</sub>`;
  }

  // 3) 常见工程符号后缀：Qp/Qpp, vp, pvar/pfix/pperf, TDSin/TDSout
  const suffixMatch = text.match(/^([A-Za-z]{1,5})(in|out|pp|var|fix|perf)$/);
  if (suffixMatch) {
    return `<i>${suffixMatch[1]}</i><sub>${suffixMatch[2]}</sub>`;
  }

  return text;
}

function handleGlobalKeyDown(e) {
  if (e.key === "Escape") {
    if (connectMode.value) {
      cancelConnectMode();
      statusMessage.value = "已取消连线模式";
    }
    if (reparentMode.value) {
      cancelReparentMode();
      statusMessage.value = "已取消子本体模式";
    }
  }
}

onMounted(() => {
  loadGraph();
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);
  window.addEventListener("keydown", handleGlobalKeyDown);
});

onUnmounted(() => {
  window.removeEventListener("pointermove", onPointerMove);
  window.removeEventListener("pointerup", onPointerUp);
  window.removeEventListener("keydown", handleGlobalKeyDown);
});
</script>

<template>
  <div class="app-container" @click="hideContextMenu">
    <!-- 全局关系数据列表，供所有的 input list="rel-list" 使用 -->
    <datalist id="rel-list">
      <option v-for="r in uniqueRelations" :key="r" :value="r"></option>
    </datalist>

    <aside class="sidebar" @click.stop>
      <div class="sidebar-header">
        <div class="logo">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"></circle><circle cx="6" cy="12" r="3"></circle><circle cx="18" cy="19" r="3"></circle><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"></line><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"></line></svg>
        </div>
        <h2>本体建模系统</h2>
      </div>
      
      <div class="sidebar-content">
        <div class="action-group">
          <h3 class="group-title">画布操作</h3>
          <button class="btn btn-primary" @click="addOntology(null, null)">
            <span class="icon">＋</span> 新增独立本体
          </button>
          <button class="btn btn-secondary" @click="syncGraph">
            <span class="icon">💾</span> 保存当前画布
          </button>
          <button class="btn btn-danger" @click="clearGraph">
            <span class="icon">🗑️</span> 一键清空画布
          </button>
        </div>

        <div class="action-group">
          <h3 class="group-title">关系连接</h3>
          <button 
            class="btn btn-outline" 
            :class="{ active: connectMode }" 
            :disabled="!selectedNodeId && !connectMode"
            @click="connectMode ? cancelConnectMode() : enableConnectMode()"
          >
            <span class="icon">🔗</span> {{ connectMode ? '取消连线 (Esc)' : '开启连线模式' }}
          </button>
          <button
            class="btn btn-outline"
            :class="{ active: reparentMode }"
            :disabled="!selectedNodeId && !reparentMode"
            @click="reparentMode ? cancelReparentMode() : enableReparentMode()"
          >
            <span class="icon">🌳</span> {{ reparentMode ? '取消子本体模式' : '转为子本体模式' }}
          </button>
          <button class="btn btn-secondary" @click="rulesDialogVisible = true">
            <span class="icon">⚙️</span> 关系逻辑规则设置
          </button>
          <p class="tip-text" v-if="connectMode">请在画布点击目标本体</p>
          <p class="tip-text" v-if="reparentMode">先选待移动本体，再点目标父本体</p>
        </div>

        <div class="action-group">
          <h3 class="group-title">OCR 导入</h3>
          <button class="btn btn-ghost" :disabled="ocrLoading" @click="useSampleOcrFile">
            <span class="icon">🧪</span> 使用示例文件 test.png
          </button>
          <input
            class="file-input"
            type="file"
            accept=".png,.jpg,.jpeg,.bmp,.webp,.txt,.csv"
            @change="onOcrFileChange"
          />
          <button class="btn btn-secondary" :disabled="!ocrFile || ocrLoading" @click="runOcrExtract('local')">
            <span class="icon">🔍</span> {{ ocrLoading ? "识别中..." : "本地识别" }}
          </button>
          <button class="btn btn-primary" :disabled="!ocrFile || ocrLoading || !canUseLlmOcr()" @click="runOcrExtract('llm')">
            <span class="icon">🤖</span> {{ ocrLoading ? "识别中..." : "大模型识别" }}
          </button>
          <button class="btn btn-outline" v-if="ocrResult" @click="ocrDialogVisible = true">
            <span class="icon">📋</span> 查看识别结果
          </button>
        </div>
      </div>

      <div class="sidebar-footer">
        <div class="status-panel" :class="{ error: statusMessage.includes('失败'), success: statusMessage.includes('已') }">
          <div class="status-indicator"></div>
          <span class="status-text">{{ statusMessage }}</span>
        </div>
        <button class="btn btn-ghost" @click="loadGraph">↻ 刷新画布</button>
      </div>
    </aside>

    <main 
      ref="canvasRef"
      class="canvas" 
      :style="{ backgroundPosition: `${viewOffset.x}px ${viewOffset.y}px`, backgroundSize: `${40 * zoom}px ${40 * zoom}px` }"
      @pointerdown="startPanning"
      @wheel.prevent="onWheel"
      @click.self="selectedNodeId = null; selectedEdgeId = null; hideContextMenu()" 
      @contextmenu.prevent="showContextMenu($event, 'canvas')"
    >
      <div class="canvas-hint" v-if="nodes.length === 0">
        画布为空，右键点击空白处「新增独立本体」
      </div>

      <!-- 右键菜单 -->
      <div class="context-menu" v-if="contextMenu.visible" :style="{ top: contextMenu.y + 'px', left: contextMenu.x + 'px' }" @click.stop>
        <template v-if="contextMenu.type === 'node'">
          <div class="menu-item" @click="addChildOntology(contextMenu.nodeId)">
            <span class="menu-icon">➕</span> 新增子本体
          </div>
          <div class="menu-item" @click="startConnectionFromMenu(contextMenu.nodeId)">
            <span class="menu-icon">🔗</span> 从此节点连线
          </div>
          <div class="menu-item" @click="startReparentFromMenu(contextMenu.nodeId)">
            <span class="menu-icon">🌳</span> 将此节点转为子本体
          </div>
          <div class="menu-divider" v-if="hasChildren(contextMenu.nodeId)"></div>
          <div class="menu-item" v-if="hasChildren(contextMenu.nodeId)" @click="toggleCollapse(contextMenu.nodeId)">
            <span class="menu-icon">↕️</span> {{ collapsedNodeIds.has(contextMenu.nodeId) ? '展开子节点' : '折叠子节点' }}
          </div>
        </template>
        <template v-else>
          <div class="menu-item" @click="addRootOntologyMenu">
            <span class="menu-icon">➕</span> 在此新增独立本体
          </div>
        </template>
      </div>

      <!-- 自定义 Prompt 弹窗 -->
      <div class="prompt-overlay" v-if="promptState.visible" @click.self="cancelPrompt">
        <div class="prompt-dialog">
          <h3>{{ promptState.title }}</h3>
          <input
            ref="promptInputRef"
            type="text"
            v-model="promptState.value"
            :placeholder="promptState.placeholder"
            @keyup.enter="submitPrompt"
            @keyup.esc="cancelPrompt"
          />
          <div class="prompt-actions">
            <button class="btn btn-secondary" @click="cancelPrompt">取消</button>
            <button class="btn btn-primary" @click="submitPrompt">确定</button>
          </div>
        </div>
      </div>

      <!-- 自定义 Confirm 弹窗 -->
      <div class="prompt-overlay" v-if="confirmState.visible" @click.self="handleConfirm(false)">
        <div class="prompt-dialog">
          <h3>{{ confirmState.title }}</h3>
          <p class="confirm-message">{{ confirmState.message }}</p>
          <div class="prompt-actions">
            <button class="btn btn-secondary" @click="handleConfirm(false)">取消</button>
            <button class="btn btn-danger" @click="handleConfirm(true)">确定清空</button>
          </div>
        </div>
      </div>

      <!-- 关系规则管理弹窗 -->
      <div class="prompt-overlay" v-if="rulesDialogVisible" @click.self="rulesDialogVisible = false">
        <div class="prompt-dialog rules-dialog" @click.stop>
          <div class="ocr-dialog-header">
            <h3>关系逻辑规则设置</h3>
            <button class="btn btn-ghost ocr-close-btn" @click="rulesDialogVisible = false">关闭</button>
          </div>

          <div class="rule-section">
            <h4 class="rule-title">互斥约束</h4>
            <p class="rule-desc">定义不能同时存在的两种关系（例如：定义了"恋人"就不能定义"父子"）</p>
            <div class="rule-add-row">
              <input type="text" list="rel-list" class="input-field" v-model="newMutex.rel1" placeholder="关系 A (如 恋人)" />
              <span class="rule-operator">与</span>
              <input type="text" list="rel-list" class="input-field" v-model="newMutex.rel2" placeholder="关系 B (如 父子)" />
              <button class="btn btn-primary" @click="addMutexRule">添加互斥</button>
            </div>
            <ul class="rule-list">
              <li class="rule-item" v-for="rule in mutexRules" :key="rule.id">
                <span><span class="tag">{{ rule.rel1 }}</span> 互斥于 <span class="tag">{{ rule.rel2 }}</span></span>
                <button class="btn btn-danger btn-sm" @click="removeMutexRule(rule.id)">删除</button>
              </li>
              <li class="rule-item empty" v-if="mutexRules.length === 0">暂无互斥规则</li>
            </ul>
          </div>

          <div class="rule-section">
            <h4 class="rule-title">顺承/推理规则</h4>
            <p class="rule-desc">定义关系传递产生的新关系（例如："父子" + "父子" = "爷孙"）</p>
            <div class="rule-add-row">
              <input type="text" list="rel-list" class="input-field" v-model="newInference.rel1" placeholder="关系 1 (如 父子)" />
              <span class="rule-operator">+</span>
              <input type="text" list="rel-list" class="input-field" v-model="newInference.rel2" placeholder="关系 2 (如 父子)" />
              <span class="rule-operator">=</span>
              <input type="text" list="rel-list" class="input-field" v-model="newInference.inferred_rel" placeholder="推理结果 (如 爷孙)" />
              <button class="btn btn-primary" @click="addInferenceRule">添加推理</button>
            </div>
            <ul class="rule-list">
              <li class="rule-item" v-for="rule in inferenceRules" :key="rule.id">
                <span><span class="tag">{{ rule.rel1 }}</span> + <span class="tag">{{ rule.rel2 }}</span> = <span class="tag highlight">{{ rule.inferred_rel }}</span></span>
                <button class="btn btn-danger btn-sm" @click="removeInferenceRule(rule.id)">删除</button>
              </li>
              <li class="rule-item empty" v-if="inferenceRules.length === 0">暂无推理规则</li>
            </ul>
          </div>
        </div>
      </div>

      <!-- OCR 结果弹窗 -->
      <div class="prompt-overlay" v-if="ocrDialogVisible" @click.self="ocrDialogVisible = false">
        <div class="prompt-dialog ocr-dialog" @click.stop>
          <div class="ocr-dialog-header">
            <h3>OCR 识别结果</h3>
            <button class="btn btn-ghost ocr-close-btn" @click="ocrDialogVisible = false">关闭</button>
          </div>
          
          <!-- 并排布局容器 -->
          <div class="ocr-split-layout">
            <!-- 左侧：原始文本预览 -->
            <div class="ocr-preview-panel">
              <div class="ocr-meta">用户上传文件预览</div>
              <div class="ocr-preview-content">
                <img v-if="ocrPreviewType === 'image'" :src="ocrPreviewUrl" alt="预览图" class="ocr-preview-img" />
                <pre v-else-if="ocrPreviewType === 'text'">{{ ocrPreviewText }}</pre>
                <div v-else class="ocr-preview-empty">暂无预览</div>
              </div>
            </div>

            <!-- 右侧：识别实体选择 -->
            <div class="ocr-panel" v-if="ocrResult">
              <div class="ocr-meta">提取实体 (模式：{{ ocrResult.mode }}，共 {{ ocrCandidates.length }} 项)</div>
              <div class="ocr-actions">
                <button class="btn btn-ghost" @click="toggleAllOcrCandidates(true)">全选</button>
                <button class="btn btn-ghost" @click="toggleAllOcrCandidates(false)">全不选</button>
              </div>
              <div class="ocr-list">
                <label class="ocr-item" v-for="(item, idx) in ocrCandidates" :key="`${item.text}-${idx}`">
                  <input type="checkbox" v-model="item.selected" />
                    <span :title="item.text">{{ item.text }}</span>
                </label>
              </div>
              <button class="btn btn-primary" :disabled="selectedOcrEntities.length === 0" @click="importOcrToCanvas">
                一键导入勾选项
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- 画布内容容器，支持平移和缩放 -->
      <div 
        class="canvas-content" 
        :style="{ transform: `translate(${viewOffset.x}px, ${viewOffset.y}px) scale(${zoom})`, transformOrigin: '0 0' }"
        @click.self="selectedNodeId = null; selectedEdgeId = null; hideContextMenu()"
      >

        <svg class="edges-layer">
          <defs>
            <marker id="arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
              <path d="M0,0 L0,8 L10,4 z" fill="#94a3b8" />
            </marker>
            <marker id="arrow-selected" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
              <path d="M0,0 L0,8 L10,4 z" fill="#3b82f6" />
            </marker>
            <marker id="arrow-parent" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
              <path d="M0,0 L0,8 L10,4 z" fill="#10b981" />
            </marker>
            <marker id="arrow-symmetric" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
              <path d="M0,0 L0,8 L10,4 z" fill="#8b5cf6" />
            </marker>
            <marker id="arrow-reverse-symmetric" markerWidth="10" markerHeight="8" refX="1" refY="4" orient="auto">
              <path d="M10,0 L10,8 L0,4 z" fill="#8b5cf6" />
            </marker>
          </defs>
          <g 
            v-for="edge in edgeViews" 
            :key="edge.id" 
            class="edge-group"
            :class="{ selected: selectedEdgeId === edge.id }"
            @click.stop="selectedEdgeId = edge.id; selectedNodeId = null; hideContextMenu()"
          >
            <!-- 增加一个宽的透明路径方便点击 -->
            <path
              :d="edge.path"
              class="edge-click-area"
              fill="none"
              stroke="transparent"
              stroke-width="20"
            />
            <path
              :d="edge.path"
              :class="['edge-line', edge.kind, { 
                symmetric: edge.characteristics?.includes('symmetric'),
                transitive: edge.characteristics?.includes('transitive')
              }]"
              :marker-end="edge.characteristics?.includes('symmetric') ? 'url(#arrow-symmetric)' : `url(#arrow${edge.kind === 'parent-child' ? '-parent' : (selectedEdgeId === edge.id ? '-selected' : '')})`"
              :marker-start="edge.characteristics?.includes('symmetric') ? 'url(#arrow-reverse-symmetric)' : ''"
              fill="none"
            />
            <rect
              :x="(edge.x1 + edge.x2) / 2 - 24"
              :y="(edge.y1 + edge.y2) / 2 - 12"
              width="48"
              height="24"
              rx="12"
              class="edge-label-bg"
            />
            <text
              :x="(edge.x1 + edge.x2) / 2"
              :y="(edge.y1 + edge.y2) / 2 + 4"
              class="edge-label"
            >
              {{ edge.relation }}
            </text>
          </g>
        </svg>

        <div
          v-for="node in visibleNodes"
          :key="node.id"
          :class="['node-card', { selected: selectedNodeId === node.id, connecting: connectMode && connectSourceId === node.id }]"
          :style="{ transform: `translate(${node.x}px, ${node.y}px)` }"
          @click.stop="onClickNode(node.id); hideContextMenu()"
          @pointerdown.stop="startDrag($event, node); hideContextMenu()"
          @contextmenu.prevent.stop="showContextMenu($event, 'node', node.id)"
        >
          <div class="node-header">
            <div class="node-icon">
              <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect></svg>
            </div>
            <span class="node-title" :title="node.name" v-html="formatSymbol(node.name)"></span>
          </div>
          <div class="node-body">
            <div class="node-id">ID: {{ node.id.slice(0, 6) }}</div>
          </div>

          <!-- 展开/折叠按钮 -->
          <div 
            v-if="hasChildren(node.id)" 
            class="collapse-btn" 
            @click.stop="toggleCollapse(node.id)"
          >
            {{ collapsedNodeIds.has(node.id) ? '+' : '-' }}
          </div>
        </div>
      </div>
    </main>

    <!-- 右侧详情面板 (Protégé 风格) -->
    <aside class="detail-panel" v-if="selectedNode || selectedEdge" @click.stop>
      <div class="panel-header">
        <h3>{{ selectedNode ? '本体详情' : '关系详情' }}</h3>
        <button class="close-btn" @click="selectedNodeId = null; selectedEdgeId = null">×</button>
      </div>

      <div class="panel-content">
        <!-- 节点编辑 -->
        <template v-if="selectedNode">
          <div class="detail-group">
            <label>名称</label>
            <input type="text" v-model="selectedNode.name" @change="syncGraph" class="input-field" />
          </div>

          <div class="detail-group">
            <div class="group-header">
              <label>属性 (Data Properties)</label>
              <button class="add-btn" @click="addAttribute">＋</button>
            </div>
            <div class="attr-list">
              <div v-for="(attr, idx) in selectedNode.attributes" :key="idx" class="attr-item">
                <input type="text" v-model="attr.key" @change="updateAttribute" placeholder="键" />
                <input type="text" v-model="attr.value" @change="updateAttribute" placeholder="值" />
                <button class="remove-btn" @click="removeAttribute(idx)">×</button>
              </div>
              <div v-if="!selectedNode.attributes?.length" class="empty-tip">暂无属性</div>
            </div>
          </div>
        </template>

        <!-- 边编辑 -->
        <template v-else-if="selectedEdge">
          <div class="detail-group">
            <label>关系名称</label>
            <input type="text" list="rel-list" v-model="selectedEdge.relation" @change="syncGraph" class="input-field" />
          </div>

          <div class="detail-group" v-if="selectedEdge.kind === 'relation'">
            <label>特征 (Characteristics)</label>
            <div class="chara-options">
              <label class="chara-item">
                <input type="checkbox" :checked="selectedEdge.characteristics?.includes('symmetric')" @change="toggleCharacteristic('symmetric')" />
                Symmetric (对称)
              </label>
              <label class="chara-item">
                <input type="checkbox" :checked="selectedEdge.characteristics?.includes('transitive')" @change="toggleCharacteristic('transitive')" />
                Transitive (传递)
              </label>
              <label class="chara-item">
                <input type="checkbox" :checked="selectedEdge.characteristics?.includes('functional')" @change="toggleCharacteristic('functional')" />
                Functional (函数)
              </label>
            </div>
          </div>
        </template>
      </div>

      <div class="panel-footer">
        <button class="btn btn-danger" @click="selectedNode ? deleteSelectedNode() : deleteSelectedEdge()">
          <span class="icon">🗑️</span> 删除此{{ selectedNode ? '本体' : '关系' }}
        </button>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.app-container {
  display: flex;
  height: 100vh;
  width: 100vw;
  background-color: #f8fafc;
  overflow: hidden;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  color: #0f172a;
}

/* Sidebar */
.sidebar {
  width: 300px;
  background: #ffffff;
  border-right: 1px solid #e2e8f0;
  display: flex;
  flex-direction: column;
  box-shadow: 4px 0 24px rgba(15, 23, 42, 0.04);
  z-index: 10;
}

.sidebar-header {
  padding: 24px;
  display: flex;
  align-items: center;
  gap: 12px;
  border-bottom: 1px solid #f1f5f9;
}

.logo {
  width: 36px;
  height: 36px;
  background: #eff6ff;
  color: #2563eb;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.logo svg {
  width: 22px;
  height: 22px;
}

.sidebar-header h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  color: #0f172a;
}

.sidebar-content {
  flex: 1;
  padding: 24px;
  overflow-y: auto;
}

.action-group {
  margin-bottom: 32px;
}

.group-title {
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #475569;
  margin: 0 0 16px 4px;
  font-weight: 700;
}

/* Buttons */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  gap: 8px;
  padding: 10px 16px;
  margin-bottom: 12px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s ease;
  border: 1px solid transparent;
  outline: none;
}

.btn:focus-visible {
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.5);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-primary {
  background: #2563eb;
  color: #ffffff;
  box-shadow: 0 4px 6px -1px rgba(37, 99, 235, 0.2);
}

.btn-primary:hover:not(:disabled) {
  background: #1d4ed8;
  box-shadow: 0 6px 8px -1px rgba(37, 99, 235, 0.3);
  transform: translateY(-1px);
}

.btn-secondary {
  background: #f1f5f9;
  color: #1e293b;
  border-color: #cbd5e1;
}

.btn-secondary:hover:not(:disabled) {
  background: #e2e8f0;
  color: #0f172a;
}

.btn-danger {
  background: #fef2f2;
  color: #dc2626;
  border-color: #fecaca;
}

.btn-danger:hover:not(:disabled) {
  background: #fee2e2;
  color: #b91c1c;
  border-color: #fca5a5;
}

.tip-text {
  font-size: 13px;
  color: #d97706;
  margin: 4px 0 0 0;
  text-align: center;
  font-weight: 500;
}

.file-input {
  display: block;
  width: 100%;
  margin-bottom: 12px;
  font-size: 13px;
  color: #334155;
  padding: 8px;
  border: 1px dashed #cbd5e1;
  border-radius: 8px;
  background: #fafafa;
}

.file-input:focus-visible {
  outline: none;
  border-color: #2563eb;
}

.ocr-dialog {
  width: 90vw !important;
  max-width: 1400px !important;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
}

.ocr-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
  padding: 0 4px;
}

.ocr-dialog-header h3 {
  margin: 0;
  font-size: 22px;
  font-weight: 800;
  color: #0f172a;
}

.ocr-close-btn {
  width: auto;
  margin: 0;
  padding: 8px 16px;
  font-size: 14px;
}

.ocr-split-layout {
  display: flex;
  gap: 24px;
  flex: 1;
  overflow: hidden;
  padding-top: 8px;
}

.ocr-preview-panel {
  flex: 1.1;
  min-width: 0;
  border: 1px solid #cbd5e1;
  border-radius: 12px;
  background: #f8fafc;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.ocr-preview-panel .ocr-meta {
  padding: 16px 16px 0;
}

.ocr-preview-content {
  flex: 1;
  overflow: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  background: #ffffff;
  margin: 12px;
  border-radius: 8px;
  border: 1px solid #f1f5f9;
}

.ocr-preview-img {
  max-width: 100%;
  height: auto;
  border-radius: 6px;
  object-fit: contain;
  margin: auto;
}

.ocr-preview-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #94a3b8;
  font-size: 14px;
}

.ocr-preview-content pre {
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 13px;
  color: #334155;
  white-space: pre-wrap;
  word-wrap: break-word;
  line-height: 1.6;
}

.ocr-panel {
  flex: 1.3;
  border: 1px solid #cbd5e1;
  border-radius: 12px;
  padding: 20px;
  background: #f8fafc;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.ocr-meta {
  font-size: 15px;
  font-weight: 600;
  color: #334155;
  margin-bottom: 16px;
}

.ocr-actions {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}

.ocr-actions .btn {
  margin-bottom: 0;
  padding: 8px 16px;
  font-size: 13px;
  width: auto;
}

.ocr-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
  flex: 1;
  max-height: none;
  min-height: 300px;
  overflow-y: auto;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  background: #ffffff;
  padding: 16px;
  margin-bottom: 16px;
}

.ocr-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px;
  border-radius: 8px;
  background: #f8fafc;
  border: 1px solid transparent;
  transition: all 0.2s ease;
  cursor: pointer;
}

.ocr-item:hover {
  background: #f1f5f9;
  border-color: #cbd5e1;
}

/* Fallback for browsers that don't support :has */
.ocr-item.selected {
  background: #eff6ff;
  border-color: #bfdbfe;
}
.ocr-item:has(input:checked) {
  background: #eff6ff;
  border-color: #bfdbfe;
}

.ocr-item input[type="checkbox"] {
  margin-top: 2px;
  width: 18px;
  height: 18px;
  accent-color: #2563eb;
  cursor: pointer;
  flex-shrink: 0;
}

.ocr-item span {
  font-size: 14px;
  color: #1e293b;
  line-height: 1.5;
  word-break: break-word;
  font-weight: 500;
}

.btn-outline {
  background: transparent;
  color: #2563eb;
  border-color: #bfdbfe;
}

.btn-outline:hover:not(:disabled) {
  background: #eff6ff;
}

.btn-outline.active {
  background: #fef2f2;
  color: #dc2626;
  border-color: #fecaca;
}

.btn-ghost {
  background: transparent;
  color: #475569;
  border: 1px solid transparent;
}

.btn-ghost:hover {
  color: #0f172a;
  background: #f1f5f9;
  border-color: #e2e8f0;
}

/* Sidebar Footer */
.sidebar-footer {
  padding: 24px;
  border-top: 1px solid #f1f5f9;
  background: #f8fafc;
}

/* 规则管理样式 */
.rules-dialog {
  width: min(96vw, 1100px);
  max-width: 1100px;
  max-height: 88vh;
  overflow: auto;
}

.rule-section {
  margin-top: 16px;
  padding: 16px;
  background: #f8fafc;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
}

.rule-title {
  margin: 0 0 4px 0;
  font-size: 15px;
  color: #1e293b;
}

.rule-desc {
  margin: 0 0 12px 0;
  font-size: 12px;
  color: #64748b;
}

.rule-add-row {
  display: flex;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
}

.rule-add-row .input-field {
  flex: 1 1 220px;
  min-width: 180px;
  padding: 8px;
  margin-bottom: 0;
}

.rule-operator {
  font-weight: bold;
  color: #64748b;
  flex-shrink: 0;
  line-height: 38px;
}

.rule-add-row .btn {
  width: auto;
  margin-bottom: 0;
  white-space: nowrap;
}

.rule-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.rule-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: white;
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid #e2e8f0;
  font-size: 14px;
}

.rule-item.empty {
  justify-content: center;
  color: #94a3b8;
  background: transparent;
  border-style: dashed;
}

.tag {
  display: inline-block;
  padding: 2px 6px;
  background: #e2e8f0;
  color: #334155;
  border-radius: 4px;
  font-size: 13px;
}

.tag.highlight {
  background: #dbeafe;
  color: #1d4ed8;
}

.btn-sm {
  padding: 4px 8px;
  font-size: 12px;
}

.status-panel {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px;
  background: #ffffff;
  border-radius: 8px;
  border: 1px solid #cbd5e1;
  margin-bottom: 16px;
  font-size: 14px;
  font-weight: 500;
  color: #334155;
  box-shadow: 0 1px 2px rgba(0,0,0,0.05);
}

.status-indicator {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #94a3b8;
  box-shadow: 0 0 0 3px #f1f5f9;
}

.status-panel.success .status-indicator {
  background: #10b981;
  box-shadow: 0 0 0 3px #d1fae5;
}

.status-panel.error .status-indicator {
  background: #ef4444;
  box-shadow: 0 0 0 3px #fee2e2;
}

.status-text {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Canvas */
.canvas {
  position: relative;
  flex: 1;
  overflow: hidden;
  background-color: #f8fafc;
  cursor: grab;
  background-image: 
    linear-gradient(rgba(203, 213, 225, 0.5) 1px, transparent 1px),
    linear-gradient(90deg, rgba(203, 213, 225, 0.5) 1px, transparent 1px);
  background-size: 40px 40px;
}

.canvas-content {
  width: 100%;
  height: 100%;
  position: absolute;
  inset: 0;
  will-change: transform;
}

.canvas:active {
  cursor: grabbing;
}

.canvas-hint {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  color: #64748b;
  font-size: 18px;
  font-weight: 500;
  pointer-events: none;
  user-select: none;
  background: rgba(255, 255, 255, 0.8);
  padding: 12px 24px;
  border-radius: 8px;
  box-shadow: 0 4px 6px rgba(0,0,0,0.05);
}

/* Edges */
.edges-layer {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  overflow: visible;
}

.edge-line {
  stroke: #94a3b8;
  stroke-width: 2.5px;
  transition: all 0.3s;
}

.edge-line.relation {
  stroke: #3b82f6;
  stroke-dasharray: 6 6;
}

.edge-line.relation.transitive {
  stroke-width: 4px;
  stroke-dasharray: 2 4;
}

.edge-line.relation.symmetric {
  stroke: #8b5cf6;
}

.edge-group.selected .edge-line {
  stroke: #3b82f6;
  stroke-width: 4px;
}

.edge-group {
  pointer-events: all;
  cursor: pointer;
}

.edge-group:hover .edge-line {
  stroke: #3b82f6;
  stroke-width: 3.5px;
}

.edge-click-area {
  cursor: pointer;
  pointer-events: stroke;
}

.edge-label, .edge-label-bg {
  pointer-events: all;
  cursor: pointer;
}

.edge-line.parent-child {
  stroke: #10b981;
}

.edge-label-bg {
  fill: #ffffff;
  stroke: #cbd5e1;
  stroke-width: 1px;
}

.edge-label {
  font-size: 12px;
  font-weight: 600;
  fill: #334155;
  text-anchor: middle;
  dominant-baseline: middle;
}

/* Detail Panel (Protégé Style) */
.detail-panel {
  width: 320px;
  background: #ffffff;
  border-left: 1px solid #e2e8f0;
  display: flex;
  flex-direction: column;
  box-shadow: -4px 0 24px rgba(15, 23, 42, 0.04);
  z-index: 10;
  animation: panel-slide 0.3s ease-out;
}

@keyframes panel-slide {
  from { transform: translateX(100%); }
  to { transform: translateX(0); }
}

.panel-header {
  padding: 20px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid #f1f5f9;
}

.panel-header h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
}

.close-btn {
  background: none;
  border: none;
  font-size: 24px;
  color: #94a3b8;
  cursor: pointer;
  line-height: 1;
}

.close-btn:hover {
  color: #64748b;
}

.panel-content {
  padding: 24px;
  overflow-y: auto;
  flex: 1;
}

.panel-footer {
  padding: 24px;
  border-top: 1px solid #f1f5f9;
  background: #fafafa;
}

.detail-group {
  margin-bottom: 24px;
}

.group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.detail-group label {
  display: block;
  font-size: 13px;
  font-weight: 700;
  color: #475569;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 8px;
}

.input-field {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  font-size: 14px;
  outline: none;
}

.input-field:focus {
  border-color: #2563eb;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
}

.attr-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.attr-item {
  display: flex;
  gap: 8px;
  align-items: center;
}

.attr-item input {
  flex: 1;
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  font-size: 13px;
}

.add-btn, .remove-btn {
  background: #f1f5f9;
  border: none;
  border-radius: 4px;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: #64748b;
  transition: all 0.2s;
}

.add-btn:hover {
  background: #e0f2fe;
  color: #0369a1;
}

.remove-btn:hover {
  background: #fef2f2;
  color: #dc2626;
}

.empty-tip {
  font-size: 13px;
  color: #94a3b8;
  text-align: center;
  padding: 12px;
  background: #f8fafc;
  border-radius: 8px;
  border: 1px dashed #cbd5e1;
}

.chara-options {
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: #f8fafc;
  padding: 16px;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
}

.chara-item {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
  color: #334155;
  cursor: pointer;
  text-transform: none !important;
  font-weight: 500 !important;
}

.chara-item input {
  width: 16px;
  height: 16px;
  accent-color: #2563eb;
}

/* Node Card */
.node-card {
  position: absolute;
  top: 0;
  left: 0;
  width: 180px;
  background: #ffffff;
  border-radius: 12px;
  border: 1px solid #cbd5e1;
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -2px rgba(0, 0, 0, 0.05);
  cursor: move;
  user-select: none;
  transition: box-shadow 0.2s, border-color 0.2s;
  will-change: transform;
}

.node-card:hover {
  box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -4px rgba(0, 0, 0, 0.1);
  border-color: #94a3b8;
}

.node-card.selected {
  border-color: #2563eb;
  box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.15), 0 10px 15px -3px rgba(0, 0, 0, 0.1);
  z-index: 2;
}

.node-card.connecting {
  border-color: #d97706;
  box-shadow: 0 0 0 4px rgba(217, 119, 6, 0.15);
  animation: pulse-border 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
}

@keyframes pulse-border {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.node-header {
  padding: 14px 16px;
  display: flex;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid #e2e8f0;
  background: #f8fafc;
  border-radius: 12px 12px 0 0;
}

.node-icon {
  color: #64748b;
  display: flex;
}

.node-card.selected .node-header {
  background: #eff6ff;
}

.node-card.selected .node-icon {
  color: #2563eb;
}

.node-title {
  font-size: 15px;
  font-weight: 700;
  color: #0f172a;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.node-title i, .ocr-item i {
  font-family: "Times New Roman", Times, serif;
  font-style: italic;
  margin-right: 1px;
}

.node-title sub, .ocr-item sub {
  font-size: 0.75em;
  bottom: -0.2em;
  margin-left: 1px;
  color: #475569;
}

.node-body {
  padding: 12px 16px;
}

.node-id {
  font-size: 12px;
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

/* 节点折叠按钮 */
.collapse-btn {
  position: absolute;
  bottom: -12px;
  left: 50%;
  transform: translateX(-50%);
  width: 24px;
  height: 24px;
  background: #ffffff;
  border: 1px solid #cbd5e1;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-weight: bold;
  color: #475569;
  cursor: pointer;
  z-index: 10;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.1);
  transition: all 0.2s;
}

.collapse-btn:hover {
  border-color: #2563eb;
  color: #2563eb;
  transform: translateX(-50%) scale(1.1);
}

/* 右键菜单 */
.context-menu {
  position: absolute;
  background: #ffffff;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1);
  padding: 8px;
  min-width: 180px;
  z-index: 1000;
  animation: context-enter 0.15s ease-out;
}

@keyframes context-enter {
  from { opacity: 0; transform: scale(0.95); }
  to { opacity: 1; transform: scale(1); }
}

.menu-item {
  padding: 10px 14px;
  font-size: 14px;
  font-weight: 500;
  color: #1e293b;
  cursor: pointer;
  border-radius: 6px;
  display: flex;
  align-items: center;
  gap: 10px;
  transition: background 0.1s;
}

.menu-item:hover {
  background: #f1f5f9;
  color: #0f172a;
}

.menu-icon {
  font-size: 16px;
}

.menu-divider {
  height: 1px;
  background: #cbd5e1;
  margin: 6px 0;
}

/* Prompt Modal */
.prompt-overlay {
  position: absolute;
  inset: 0;
  background: rgba(15, 23, 42, 0.5);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.prompt-dialog {
  background: #ffffff;
  padding: 32px;
  border-radius: 16px;
  width: 800px;
  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
  animation: modal-enter 0.2s ease-out;
}

@keyframes modal-enter {
  from { opacity: 0; transform: scale(0.95) translateY(10px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}

.prompt-dialog h3 {
  margin: 0 0 20px 0;
  font-size: 18px;
  font-weight: 700;
  color: #0f172a;
}

.confirm-message {
  font-size: 15px;
  color: #475569;
  line-height: 1.6;
  margin-bottom: 24px;
}

.prompt-dialog input {
  width: 100%;
  padding: 12px 14px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  font-size: 15px;
  color: #0f172a;
  outline: none;
  transition: border-color 0.2s, box-shadow 0.2s;
  margin-bottom: 24px;
  background: #f8fafc;
}

.prompt-dialog input:focus {
  border-color: #2563eb;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
  background: #ffffff;
}

.prompt-actions {
  display: flex;
  gap: 12px;
  justify-content: flex-end;
}

.prompt-actions .btn {
  width: auto;
  margin: 0;
  padding: 10px 20px;
}

</style>
