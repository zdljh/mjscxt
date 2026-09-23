import React, { useState, useEffect, useRef, useCallback } from 'react';
import { relationsApi } from '@/api/client';
import type { Relation } from '@/types';

// 关系类型颜色映射
const RELATION_COLORS: Record<string, string> = {
  family: '#ef4444', // 红色 - 亲属
  friend: '#22c55e', // 绿色 - 朋友
  enemy: '#6b7280', // 灰色 - 敌人
  romance: '#ec4899', // 粉色 - 恋人
  mentor: '#f59e0b', // 橙色 - 师徒
  colleague: '#3b82f6', // 蓝色 - 同事
  rival: '#8b5cf6', // 紫色 - 对手
  ally: '#14b8a6', // 青色 - 盟友
  stranger: '#9ca3af', // 浅灰 - 陌生人
  master: '#dc2626', // 深红 - 主仆
};

const RELATION_LABELS: Record<string, string> = {
  family: '亲属',
  friend: '朋友',
  enemy: '敌人',
  romance: '恋人',
  mentor: '师徒',
  colleague: '同事',
  rival: '对手',
  ally: '盟友',
  stranger: '陌生人',
  master: '主仆',
};

interface GraphNode {
  id: string;
  name: string;
  role?: string;
  x: number;
  y: number;
  vx?: number;
  vy?: number;
}

interface GraphEdge {
  source: string;
  target: string;
  type: string;
  strength: number;
  label?: string;
}

interface RelationGraphTabProps {
  projectKey: string;
}

export function RelationGraphTab({ projectKey }: RelationGraphTabProps) {
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [hoveredEdge, setHoveredEdge] = useState<string | null>(null);
  
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const animationRef = useRef<number | null>(null);
  const isDragging = useRef(false);
  const draggedNode = useRef<GraphNode | null>(null);
  const dragOffset = useRef({ x: 0, y: 0 });

  // 加载关系数据
  useEffect(() => {
    if (!projectKey) return;
    
    setLoading(true);
    relationsApi.graph(projectKey)
      .then(data => {
        const graphData = data as { nodes: GraphNode[]; edges: GraphEdge[] };
        setNodes(graphData.nodes || []);
        setEdges(graphData.edges || []);
      })
      .catch(err => {
        setError(err instanceof Error ? err.message : '加载关系图失败');
      })
      .finally(() => {
        setLoading(false);
      });
  }, [projectKey]);

  // 简单的力导向布局模拟
  const simulateLayout = useCallback(() => {
    if (nodes.length === 0) return;

    const alpha = 0.3;
    const centerX = 400;
    const centerY = 300;
    
    // 初始化位置（如果没有）
    let updatedNodes = nodes.map(n => ({
      ...n,
      x: n.x ?? centerX + (Math.random() - 0.5) * 200,
      y: n.y ?? centerY + (Math.random() - 0.5) * 200,
      vx: n.vx ?? 0,
      vy: n.vy ?? 0,
    }));

    // 节点间斥力
    for (let i = 0; i < updatedNodes.length; i++) {
      for (let j = i + 1; j < updatedNodes.length; j++) {
        const a = updatedNodes[i];
        const b = updatedNodes[j];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const force = (alpha * 500) / dist;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        
        if (!isDragging.current || draggedNode?.current?.id !== a.id) {
          a.vx -= fx;
          a.vy -= fy;
        }
        if (!isDragging.current || draggedNode?.current?.id !== b.id) {
          b.vx += fx;
          b.vy += fy;
        }
      }
    }

    // 边的引力
    for (const edge of edges) {
      const source = updatedNodes.find(n => n.id === edge.source);
      const target = updatedNodes.find(n => n.id === edge.target);
      if (!source || !target) continue;

      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = (alpha * dist) / 10;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;

      if (!isDragging.current || draggedNode?.current?.id !== source.id) {
        source.vx += fx;
        source.vy += fy;
      }
      if (!isDragging.current || draggedNode?.current?.id !== target.id) {
        target.vx -= fx;
        target.vy -= fy;
      }
    }

    // 中心引力
    for (const node of updatedNodes) {
      if (isDragging.current && draggedNode?.current?.id === node.id) continue;
      
      const dx = centerX - node.x;
      const dy = centerY - node.y;
      node.vx += dx * 0.001;
      node.vy += dy * 0.001;
    }

    // 应用速度并限制边界
    updatedNodes = updatedNodes.map(n => {
      if (isDragging.current && draggedNode?.current?.id === n.id) return n;
      
      const speed = 0.8;
      const vx = Math.max(-speed, Math.min(speed, n.vx * 0.9));
      const vy = Math.max(-speed, Math.min(speed, n.vy * 0.9));
      return {
        ...n,
        x: Math.max(50, Math.min(750, n.x + vx)),
        y: Math.max(50, Math.min(550, n.y + vy)),
        vx,
        vy,
      };
    });

    setNodes(updatedNodes);
  }, [nodes, edges]);

  // 动画循环
  useEffect(() => {
    const animate = () => {
      simulateLayout();
      animationRef.current = requestAnimationFrame(animate);
    };
    
    animationRef.current = requestAnimationFrame(animate);
    return () => {
      if (animationRef.current) {
        cancelAnimationFrame(animationRef.current);
      }
    };
  }, [simulateLayout]);

  // SVG 事件处理
  const handleMouseDown = (e: React.MouseEvent, node: GraphNode) => {
    e.preventDefault();
    isDragging.current = true;
    draggedNode.current = node;
    
    const svg = svgRef.current;
    if (svg) {
      const rect = svg.getBoundingClientRect();
      dragOffset.current = {
        x: e.clientX - rect.left - node.x,
        y: e.clientY - rect.top - node.y,
      };
    }
  };

  const handleMouseMove = (e: React.MouseEvent) => {
    if (!isDragging.current || !draggedNode.current || !svgRef.current) return;
    
    const rect = svgRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left - dragOffset.current.x;
    const y = e.clientY - rect.top - dragOffset.current.y;
    
    draggedNode.current.x = Math.max(50, Math.min(750, x));
    draggedNode.current.y = Math.max(50, Math.min(550, y));
    draggedNode.current.vx = 0;
    draggedNode.current.vy = 0;
    
    setNodes([...nodes]);
  };

  const handleMouseUp = () => {
    isDragging.current = false;
    draggedNode.current = null;
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-gray-500">加载中...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg text-red-400">
        {error}
      </div>
    );
  }

  if (nodes.length === 0) {
    return (
      <div className="text-center py-12">
        <div className="text-5xl mb-4">🔗</div>
        <h4 className="text-lg font-medium text-gray-900 mb-1">暂无关系数据</h4>
        <p className="text-sm text-gray-500">请先在角色管理中创建角色关系</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-semibold text-gray-900">角色关系图谱</h3>
        <div className="flex gap-2 text-xs">
          {Object.entries(RELATION_LABELS).slice(0, 5).map(([type, label]) => (
            <span key={type} className="flex items-center gap-1">
              <span className="w-3 h-3 rounded-full" style={{ backgroundColor: RELATION_COLORS[type] }}></span>
              {label}
            </span>
          ))}
        </div>
      </div>

      <div
        ref={containerRef}
        className="relative bg-white rounded-lg border border-gray-200 overflow-hidden"
        style={{ height: '500px' }}
      >
        <svg
          ref={svgRef}
          width="100%"
          height="100%"
          viewBox="0 0 800 600"
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onMouseLeave={handleMouseUp}
          className="cursor-grab active:cursor-grabbing"
        >
          {/* 背景网格 */}
          <defs>
            <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
              <path d="M 40 0 L 0 0 0 40" fill="none" stroke="currentColor" strokeWidth="0.5" className="text-gray-100" />
            </pattern>
          </defs>
          <rect width="100%" height="100%" fill="url(#grid)" />

          {/* 边 */}
          {edges.map((edge, idx) => {
            const source = nodes.find(n => n.id === edge.source);
            const target = nodes.find(n => n.id === edge.target);
            if (!source || !target) return null;

            const color = RELATION_COLORS[edge.type] || '#9ca3af';
            const width = Math.abs(edge.strength) * 3 + 1;
            const isHovered = hoveredEdge === `${edge.source}-${edge.target}`;

            return (
              <g key={idx}>
                <line
                  x1={source.x}
                  y1={source.y}
                  x2={target.x}
                  y2={target.y}
                  stroke={color}
                  strokeWidth={isHovered ? width + 2 : width}
                  opacity={isHovered ? 1 : 0.6}
                  onMouseEnter={() => setHoveredEdge(`${edge.source}-${edge.target}`)}
                  onMouseLeave={() => setHoveredEdge(null)}
                  className="cursor-pointer"
                />
                {/* 边的中点标签 */}
                {isHovered && (
                  <g>
                    <rect
                      x={(source.x + target.x) / 2 - 30}
                      y={(source.y + target.y) / 2 - 12}
                      width="60"
                      height="20"
                      fill="rgba(0,0,0,0.7)"
                      rx="4"
                    />
                    <text
                      x={(source.x + target.x) / 2}
                      y={(source.y + target.y) / 2}
                      textAnchor="middle"
                      dominantBaseline="middle"
                      fill="white"
                      fontSize="10"
                    >
                      {RELATION_LABELS[edge.type] || edge.type}
                    </text>
                  </g>
                )}
              </g>
            );
          })}

          {/* 节点 */}
          {nodes.map((node) => {
            const isSelected = selectedNode?.id === node.id;
            return (
              <g
                key={node.id}
                transform={`translate(${node.x}, ${node.y})`}
                onMouseDown={(e) => handleMouseDown(e, node)}
                onClick={() => setSelectedNode(node)}
                className="cursor-pointer"
              >
                {/* 节点光环（选中时） */}
                {isSelected && (
                  <circle
                    r={35}
                    fill="none"
                    stroke="#6366f1"
                    strokeWidth="3"
                    opacity="0.5"
                  />
                )}
                
                {/* 节点圆形 */}
                <circle
                  r={25}
                  fill={isSelected ? '#6366f1' : 'white'}
                  stroke={isSelected ? '#4f46e5' : '#d1d5db'}
                  strokeWidth="2"
                  className="transition-all duration-200"
                />
                
                {/* 角色首字母 */}
                <text
                  textAnchor="middle"
                  dominantBaseline="middle"
                  fill={isSelected ? 'white' : '#374151'}
                  fontSize="14"
                  fontWeight="bold"
                >
                  {node.name?.[0] || '?'}
                </text>
                
                {/* 角色名称 */}
                <text
                  y={40}
                  textAnchor="middle"
                  fill="#374151"
                  fontSize="11"
                  className=""
                >
                  {node.name}
                </text>
                
                {/* 角色类型 */}
                {node.role && (
                  <text
                    y={52}
                    textAnchor="middle"
                    fill="#9ca3af"
                    fontSize="9"
                  >
                    {node.role}
                  </text>
                )}
              </g>
            );
          })}
        </svg>

        {/* 节点详情面板 */}
        {selectedNode && (
          <div className="absolute top-4 right-4 w-64 bg-white rounded-lg shadow-lg border border-gray-200 p-4">
            <div className="flex items-start justify-between mb-3">
              <div>
                <h4 className="font-semibold text-gray-900">{selectedNode.name}</h4>
                {selectedNode.role && (
                  <p className="text-xs text-gray-500">{selectedNode.role}</p>
                )}
              </div>
              <button
                onClick={() => setSelectedNode(null)}
                className="text-gray-400 hover:text-gray-600"
              >
                ✕
              </button>
            </div>
            
            <div className="space-y-2">
              <p className="text-xs text-gray-600">
                关系数: {edges.filter(e => e.source === selectedNode.id || e.target === selectedNode.id).length}
              </p>
              
              {/* 关联关系列表 */}
              <div className="space-y-1">
                {edges
                  .filter(e => e.source === selectedNode.id || e.target === selectedNode.id)
                  .slice(0, 5)
                  .map((edge, idx) => {
                    const otherId = edge.source === selectedNode.id ? edge.target : edge.source;
                    const otherNode = nodes.find(n => n.id === otherId);
                    if (!otherNode) return null;
                    
                    return (
                      <div
                        key={idx}
                        className="flex items-center gap-2 text-xs"
                      >
                        <span
                          className="w-2 h-2 rounded-full shrink-0"
                          style={{ backgroundColor: RELATION_COLORS[edge.type] || '#9ca3af' }}
                        ></span>
                        <span className="text-gray-700">{otherNode.name}</span>
                        <span className="text-gray-400 ml-auto">
                          {RELATION_LABELS[edge.type] || edge.type}
                        </span>
                      </div>
                    );
                  })}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 操作提示 */}
      <div className="flex items-center justify-between text-xs text-gray-500">
        <span>💡 拖拽节点调整布局 · 点击节点查看详情</span>
        <span>共 {nodes.length} 个角色 · {edges.length} 条关系</span>
      </div>
    </div>
  );
}
