window.initMonicaGraph = () => {
  'use strict';

  const COMMUNITY_COLORS = [
    '#357edd', '#19a974', '#ff6300', '#7c3aed', '#d81b60',
    '#0e7490', '#b7791f', '#5f6c7b', '#2f855a', '#c05621',
  ];
  const LAYOUT_EXPANSION = 5;
  const DEFAULT_VIEW_SCALE = 0.45;
  const REDUCED_MOTION = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const integer = value => Math.max(0, Math.round(number(value)));
  const formatNumber = value => new Intl.NumberFormat('zh-CN').format(integer(value));
  const nodeLabel = node => node.name || node.label || node.u || '未知联系人';

  // Canvas colours come from tokens.css; only plain hex tokens are read so they can take an alpha.
  const rgba = (hex, alpha) => {
    const value = hex.replace('#', '');
    const full = value.length === 3 ? value.split('').map(part => part + part).join('') : value.slice(0, 6);
    const [r, g, b] = [0, 2, 4].map(at => parseInt(full.slice(at, at + 2), 16));
    return `rgba(${r},${g},${b},${alpha})`;
  };
  const readPalette = element => {
    const style = getComputedStyle(element);
    const token = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
    return {
      text: token('--ink', '#1b2230'),
      soft: token('--ink-2', '#545d6b'),
      faint: token('--ink-3', '#8b93a1'),
      surface: token('--surface', '#ffffff'),
      accent: token('--gamboge', '#f5b83d'),
      group: token('--graph-group', '#5b7fb8'),
      cross: token('--graph-inward', '#7c5cc4'),
      communities: token('--graph-communities', COMMUNITY_COLORS.join(',')).split(',').map(part => part.trim()),
    };
  };

  document.querySelectorAll('[data-relationship-graph]').forEach(root => {
    if (root.dataset.initialized) return;
    root.dataset.initialized = 'true';
    let palette = readPalette(root);
    const clusterColor = cluster => (cluster.peripheral
      ? palette.faint
      : palette.communities[Math.abs(cluster.id) % palette.communities.length]);
    const canvas = root.querySelector('[data-graph-canvas]');
    const context = canvas.getContext('2d');
    const loadButton = root.querySelector('[data-graph-load]');
    const controls = root.querySelector('[data-graph-controls]');
    const filter = root.querySelector('[data-graph-filter]');
    const filterValue = root.querySelector('[data-graph-filter-value]');
    const searchForm = root.querySelector('[data-graph-search-form]');
    const searchInput = root.querySelector('[data-graph-search]');
    const searchOptions = root.querySelector('[data-graph-search-options]');
    const motionButton = root.querySelector('[data-graph-motion]');
    const groupToggle = root.querySelector('[data-graph-groups]');
    const resetButton = root.querySelector('[data-graph-reset]');
    const status = root.querySelector('[data-graph-status]');
    const details = root.querySelector('[data-graph-details]');
    const footnote = root.querySelector('[data-graph-footnote]');
    const pointers = new Map();
    let graph = null;
    let width = 0;
    let height = 0;
    let threshold = 0;
    let hovered = null;
    let selected = null;
    let draggingNode = null;
    let clusterDrag = null;
    let pointerStart = null;
    let dragged = false;
    let multiPointerGesture = false;
    let pinch = null;
    let message = '';
    let navigationTimer = null;
    let loading = false;
    let simulation = null;
    let simulationRunning = false;
    let includeGroups = false;
    const view = {scale: DEFAULT_VIEW_SCALE, panX: 0, panY: 0};

    canvas.style.touchAction = 'none';

    const setStatus = (text, failed = false) => {
      if (status.textContent !== text) {
        status.textContent = text;
      }
      status.classList.toggle('red', failed);
    };
    if (!context) {
      setStatus('浏览器不支持 Canvas，无法显示关系图。', true);
      loadButton.disabled = true;
      return;
    }
    if (!window.d3) {
      setStatus('D3.js 未加载，无法显示动态关系图。', true);
      loadButton.disabled = true;
      return;
    }

    const drawMessage = text => {
      message = text;
      render();
    };

    const resize = () => {
      const rectangle = canvas.getBoundingClientRect();
      width = Math.max(1, rectangle.width);
      height = Math.max(1, rectangle.height);
      const dpr = Math.max(1, window.devicePixelRatio || 1);
      const pixelWidth = Math.round(width * dpr);
      const pixelHeight = Math.round(height * dpr);
      if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
        canvas.width = pixelWidth;
        canvas.height = pixelHeight;
      }
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      render();
    };

    const graphScale = () => (
      Math.min(
        Math.max(1, width - 64) / graph.bounds.width,
        Math.max(1, height - 64) / graph.bounds.height
      )
    );

    const projection = node => {
      const bounds = graph.bounds;
      const baseScale = graphScale();
      return {
        x: width / 2 + view.panX + (node.x - bounds.centerX) * baseScale * view.scale,
        y: height / 2 + view.panY + (node.y - bounds.centerY) * baseScale * view.scale,
      };
    };

    const unproject = point => {
      const scale = graphScale() * view.scale;
      return {
        x: graph.bounds.centerX + (point.x - width / 2 - view.panX) / scale,
        y: graph.bounds.centerY + (point.y - height / 2 - view.panY) / scale,
      };
    };

    const nodeRadius = node => Math.max(1.2, (4 + Math.min(7, Math.log1p(Math.max(0, node.deg)) * 1.3)) * Math.min(1.4, Math.sqrt(view.scale)));
    const edgeWeight = edge => includeGroups ? edge.combined : edge.direct;
    const activeMaximumWeight = () => (
      includeGroups ? graph.maxCombinedWeight : graph.maxWeight
    );

    const updateMotionButton = () => {
      if (!motionButton) {
        return;
      }
      motionButton.textContent = simulationRunning ? '节点运动：开' : '节点运动：关';
      motionButton.setAttribute('aria-pressed', String(simulationRunning));
    };

    const clusterRepulsion = activeNodeIndexes => {
      let nodes = [];
      const force = alpha => {
        const groups = new Map();
        nodes.forEach(node => {
          if (!activeNodeIndexes.has(node.index)) {
            return;
          }
          if (!groups.has(node.clusterIndex)) {
            groups.set(node.clusterIndex, {
              members: [],
              x: 0,
              y: 0,
            });
          }
          const group = groups.get(node.clusterIndex);
          group.members.push(node);
          group.x += node.x;
          group.y += node.y;
        });
        const centers = [...groups.entries()].map(([clusterIndex, group]) => {
          group.x /= group.members.length;
          group.y /= group.members.length;
          group.clusterIndex = clusterIndex;
          group.radius = LAYOUT_EXPANSION
            * (0.12 + 0.024 * Math.sqrt(group.members.length));
          return group;
        });
        for (let leftIndex = 0; leftIndex < centers.length; leftIndex += 1) {
          const left = centers[leftIndex];
          for (let rightIndex = leftIndex + 1; rightIndex < centers.length; rightIndex += 1) {
            const right = centers[rightIndex];
            let dx = right.x - left.x;
            let dy = right.y - left.y;
            if (Math.abs(dx) + Math.abs(dy) < 0.0001) {
              const angle = (left.clusterIndex + right.clusterIndex + 1) * 2.399963;
              dx = Math.cos(angle) * 0.001;
              dy = Math.sin(angle) * 0.001;
            }
            const distance = Math.max(0.001, Math.hypot(dx, dy));
            const minimum = left.radius + right.radius + 0.22 * LAYOUT_EXPANSION;
            if (distance >= minimum) {
              continue;
            }
            const push = Math.min(
              0.05 * LAYOUT_EXPANSION,
              (minimum - distance) * alpha * 0.2
            );
            const pushX = dx / distance * push;
            const pushY = dy / distance * push;
            left.members.forEach(node => {
              node.vx -= pushX;
              node.vy -= pushY;
            });
            right.members.forEach(node => {
              node.vx += pushX;
              node.vy += pushY;
            });
          }
        }
      };
      force.initialize = values => {
        nodes = values;
      };
      return force;
    };

    const clusterContainment = activeNodeIndexes => {
      let nodes = [];
      const force = alpha => {
        nodes.forEach(node => {
          if (!activeNodeIndexes.has(node.index)) {
            return;
          }
          const cluster = graph.clusters[node.clusterIndex];
          const dx = node.x - cluster.targetX;
          const dy = node.y - cluster.targetY;
          const distance = Math.max(0.001, Math.hypot(dx, dy));
          if (distance <= cluster.layoutRadius) {
            return;
          }
          const pull = Math.min(
            0.06,
            (distance - cluster.layoutRadius) * alpha * 0.45
          );
          node.vx -= dx / distance * pull;
          node.vy -= dy / distance * pull;
        });
      };
      force.initialize = values => {
        nodes = values;
      };
      return force;
    };

    const configureSimulation = () => {
      if (!simulation || !graph) {
        return;
      }
      const maximum = Math.max(activeMaximumWeight(), 0.001);
      const links = graph.edges
        .filter(edge => {
          const weight = edgeWeight(edge);
          return weight > 0 && weight >= threshold;
        })
        .map(edge => ({
          source: edge.a,
          target: edge.b,
          edge,
        }));
      const activeNodeIndexes = new Set();
      links.forEach(link => {
        activeNodeIndexes.add(link.source);
        activeNodeIndexes.add(link.target);
      });
      const nodeParticipates = node => activeNodeIndexes.has(node.index);
      simulation
        .force('link', window.d3.forceLink(links)
          .id(node => node.index)
          .distance(link => {
            const weight = edgeWeight(link.edge);
            return LAYOUT_EXPANSION * (0.055
              + 0.05 / Math.sqrt(1 + weight)
              + (link.edge.crossCluster ? 0.1 : 0));
          })
          .strength(link => {
            const strength = 0.02
              + 0.12 * Math.sqrt(edgeWeight(link.edge) / maximum);
            return link.edge.crossCluster ? strength * 0.06 : strength;
          }))
        .force('charge', window.d3.forceManyBody()
          .strength(node => nodeParticipates(node)
            ? -0.008 * LAYOUT_EXPANSION * LAYOUT_EXPANSION
            : 0)
          .distanceMin(0.012 * LAYOUT_EXPANSION)
          .distanceMax(0.32 * LAYOUT_EXPANSION))
        .force('collision', window.d3.forceCollide()
          .radius(node => nodeParticipates(node) ? 0.021 * LAYOUT_EXPANSION : 0)
          .strength(0.85))
        .force('clusterRepulsion', clusterRepulsion(activeNodeIndexes))
        .force('clusterContainment', clusterContainment(activeNodeIndexes))
        .force('clusterX', window.d3.forceX(node => (
          graph.clusters[node.clusterIndex].targetX
        )).strength(node => {
          if (!nodeParticipates(node)) {
            return 0;
          }
          const cluster = graph.clusters[node.clusterIndex];
          return cluster.peripheral ? 0.025 : (cluster.isolated ? 0.2 : 0.15);
        }))
        .force('clusterY', window.d3.forceY(node => (
          graph.clusters[node.clusterIndex].targetY
        )).strength(node => {
          if (!nodeParticipates(node)) {
            return 0;
          }
          const cluster = graph.clusters[node.clusterIndex];
          return cluster.peripheral ? 0.025 : (cluster.isolated ? 0.2 : 0.15);
        }));
      if (simulationRunning) {
        simulation.alpha(Math.max(simulation.alpha(), 0.45)).restart();
      }
    };

    const createSimulation = () => {
      simulation?.stop();
      graph.nodes.forEach((node, index) => {
        node.index = index;
      });
      simulation = window.d3.forceSimulation(graph.nodes)
        .alphaDecay(0.025)
        .velocityDecay(0.35)
        .on('tick', () => {
          render();
        });
      configureSimulation();
    };

    const setSimulationRunning = running => {
      simulationRunning = Boolean(running && simulation);
      updateMotionButton();
      if (simulationRunning) {
        simulation.alphaTarget(0.025).alpha(Math.max(simulation.alpha(), 0.45)).restart();
      } else {
        simulation?.alphaTarget(0).stop();
      }
      render();
    };

    function render() {
      context.clearRect(0, 0, width, height);
      if (!graph) {
        if (message) {
          context.fillStyle = palette.faint;
          context.font = '14px sans-serif';
          context.textAlign = 'center';
          context.fillText(message, width / 2, height / 2);
        }
        return;
      }

      const visibleEdges = graph.edges.filter(edge => {
        const weight = edgeWeight(edge);
        return weight > 0 && weight >= threshold;
      });
      const visibleNodeIndexes = new Set();
      visibleEdges.forEach(edge => {
        visibleNodeIndexes.add(edge.a);
        visibleNodeIndexes.add(edge.b);
      });
      const focusActive = selected !== null && visibleNodeIndexes.has(selected);
      const focusedEdges = new Set();
      const focusedNodeIndexes = new Set();
      const focusedNodeWeights = new Map();
      if (focusActive) {
        focusedNodeIndexes.add(selected);
        visibleEdges.forEach(edge => {
          if (edge.a !== selected && edge.b !== selected) {
            return;
          }
          const neighbor = edge.a === selected ? edge.b : edge.a;
          focusedEdges.add(edge);
          focusedNodeIndexes.add(neighbor);
          focusedNodeWeights.set(neighbor, edgeWeight(edge));
        });
      }
      graph.visibleNodeIndexes = visibleNodeIndexes;
      graph.clusters.filter(cluster => !cluster.peripheral && cluster.members.length >= 3).forEach(cluster => {
        const members = cluster.members.filter(index => visibleNodeIndexes.has(index));
        if (members.length < 3) {
          return;
        }
        const points = members.map(index => projection(graph.nodes[index]));
        const center = points.reduce(
          (total, point) => ({x: total.x + point.x, y: total.y + point.y}),
          {x: 0, y: 0}
        );
        center.x /= points.length;
        center.y /= points.length;
        const radius = Math.max(
          24,
          cluster.layoutRadius * graphScale() * view.scale + 10
        );
        const clusterFocused = !focusActive
          || members.some(index => focusedNodeIndexes.has(index));
        context.beginPath();
        context.arc(center.x, center.y, radius, 0, Math.PI * 2);
        context.fillStyle = cluster.color;
        context.globalAlpha = clusterFocused
          ? (cluster.isolated ? 0.025 : 0.045)
          : 0.006;
        context.fill();
        context.globalAlpha = clusterFocused
          ? (cluster.isolated ? 0.3 : 0.14)
          : 0.025;
        context.strokeStyle = cluster.color;
        context.lineWidth = 1;
        context.setLineDash(cluster.isolated ? [5, 4] : []);
        context.stroke();
        context.setLineDash([]);
        context.globalAlpha = 1;
        if (cluster.rank < 12 && clusterFocused) {
          context.font = '600 11px sans-serif';
          context.fillStyle = cluster.color;
          context.textAlign = 'center';
          context.textBaseline = 'middle';
          context.fillText(`${cluster.label} · ${members.length}人`, center.x, center.y - radius + 13);
        }
      });

      const maximum = Math.max(activeMaximumWeight(), 0.001);
      context.lineCap = 'round';
      visibleEdges.sort((left, right) => (
        Number(right.crossCluster) - Number(left.crossCluster)
      )).forEach(edge => {
        const weight = edgeWeight(edge);
        const start = projection(graph.nodes[edge.a]);
        const end = projection(graph.nodes[edge.b]);
        const strength = Math.max(0.08, Math.min(0.55, weight / maximum));
        context.beginPath();
        context.moveTo(start.x, start.y);
        context.lineTo(end.x, end.y);
        const focused = focusedEdges.has(edge);
        if (focusActive && !focused) {
          context.setLineDash([]);
          context.strokeStyle = rgba(palette.faint, 0.025);
          context.lineWidth = 0.35;
        } else if (focused) {
          context.setLineDash(edge.groupOnly ? [5, 5] : []);
          context.strokeStyle = edge.groupOnly ? rgba(palette.group, 0.9) : rgba(palette.accent, 0.88);
          context.lineWidth = 2 + 4 * Math.sqrt(weight / maximum);
        } else if (edge.groupOnly) {
          context.setLineDash([4, 5]);
          context.strokeStyle = rgba(palette.group, 0.2);
          context.lineWidth = 0.75;
        } else {
          context.setLineDash([]);
          context.strokeStyle = edge.crossCluster
            ? rgba(palette.cross, Math.max(0.08, strength * 0.7))
            : rgba(palette.faint, strength);
          context.lineWidth = (edge.crossCluster ? 0.75 : 0.5)
            + 3 * Math.sqrt(weight / maximum);
        }
        context.stroke();
      });
      context.setLineDash([]);

      graph.nodes.forEach((node, index) => {
        if (!visibleNodeIndexes.has(index)) {
          return;
        }
        const point = projection(node);
        const radius = nodeRadius(node);
        const active = index === hovered || index === selected;
        const related = !focusActive || focusedNodeIndexes.has(index);
        context.globalAlpha = related ? 1 : 0.12;
        context.beginPath();
        context.arc(point.x, point.y, radius + (active ? 2 : 0), 0, Math.PI * 2);
        context.fillStyle = node.known ? graph.clusters[node.clusterIndex].color : rgba(palette.faint, 0.6);
        context.fill();
        context.strokeStyle = index === selected
          ? palette.text
          : (focusActive && related ? palette.accent : (active ? palette.text : palette.surface));
        context.lineWidth = index === selected ? 3 : (focusActive && related ? 2 : (active ? 2 : 1));
        context.stroke();
        context.globalAlpha = 1;
      });

      const labelIndexes = focusActive
        ? [
            selected,
            ...[...focusedNodeIndexes]
              .filter(index => index !== selected)
              .sort((left, right) => (
                (focusedNodeWeights.get(right) || 0)
                - (focusedNodeWeights.get(left) || 0)
              ))
              .slice(0, 24),
          ]
        : [...graph.labelNodes, hovered, selected];
      new Set(labelIndexes).forEach(index => {
        if (
          index === null
          || index === undefined
          || !visibleNodeIndexes.has(index)
        ) {
          return;
        }
        const node = graph.nodes[index];
        const point = projection(node);
        context.font = index === hovered ? '600 12px sans-serif' : '11px sans-serif';
        context.fillStyle = palette.soft;
        context.textAlign = 'left';
        context.textBaseline = 'middle';
        context.fillText(nodeLabel(node), point.x + nodeRadius(node) + 4, point.y);
      });

      setStatus(
        `已显示 ${formatNumber(visibleNodeIndexes.size)} / ${formatNumber(graph.displayNodeCount)} 个节点、`
        + `${formatNumber(visibleEdges.length)} / ${formatNumber(graph.edges.length)} 条可见边、`
        + `${formatNumber(graph.clusters.filter(cluster => !cluster.peripheral).length)} 个关系社群、`
        + `${formatNumber(graph.clusters.filter(cluster => cluster.isolated).length)} 个独立社群、`
        + `${formatNumber(visibleEdges.filter(edge => edge.crossCluster).length)} 条跨社群边。`
        + (focusActive ? ` 已高亮 ${formatNumber(focusedNodeIndexes.size - 1)} 个直接关系。` : '')
        + (simulationRunning ? ' 动态演算中。' : ' 节点运动已暂停。')
      );
    }

    const prepareGraph = payload => {
      if (!payload || !Array.isArray(payload.nodes) || !Array.isArray(payload.edges)) {
        throw new Error('图文件缺少 nodes 或 edges 数组');
      }
      // Names typed by hand or suggested by the model, keyed by the API's community index.
      const communityNames = new Map((payload.communities || [])
        .filter(community => community.label)
        .map(community => [integer(community.index), community.label]));
      const nodes = payload.nodes.map((node, index) => {
        const facts = Array.isArray(node.facts)
          ? node.facts
          : [node.like, node.comment, node.reply, node.mention, node.co, node.group];
        const hasDirectionalFacts = Array.isArray(node.out) && Array.isArray(node.in);
        const x = Number(node.x);
        const y = Number(node.y);
        if (!Number.isFinite(x) || !Number.isFinite(y)) {
          throw new Error(`节点 ${index} 缺少有效的预计算坐标`);
        }
        return {
          ...node,
          x,
          y,
          deg: number(node.deg),
          like: integer(facts[0]),
          comment: integer(facts[1]),
          reply: integer(facts[2]),
          mention: integer(facts[3]),
          co: integer(facts[4]),
          group: integer(facts[5]),
          known: Boolean(node.known && node.hash),
          vx: 0,
          vy: 0,
          pinned: false,
          clusterIndex: 0,
          outward: hasDirectionalFacts ? node.out.map(integer) : [0, 0, 0, 0],
          inward: hasDirectionalFacts ? node.in.map(integer) : [0, 0, 0, 0],
          hasDirectionalFacts,
          hasDirect: false,
        };
      });
      const edges = payload.edges.map(edge => {
        const ab = Array.isArray(edge.ab) ? edge.ab.map(integer) : [0, 0, 0, 0];
        const ba = Array.isArray(edge.ba) ? edge.ba.map(integer) : [0, 0, 0, 0];
        const direct = Math.max(0, number(edge.direct));
        const groupWeight = Math.max(0, number(edge.group_weight));
        const combined = Math.max(
          direct + groupWeight,
          number(edge.combined ?? edge.w)
        );
        return {
          ...edge,
          a: integer(edge.a),
          b: integer(edge.b),
          w: direct,
          direct,
          groupWeight,
          combined,
          reciprocity: Math.max(0, Math.min(1, number(edge.reciprocity))),
          ab,
          ba,
          groupOnly: direct === 0 && integer(edge.group) > 0,
        };
      }).filter(edge => (
        edge.a < nodes.length
        && edge.b < nodes.length
        && edge.a !== edge.b
        && nodes[edge.a].u !== payload.self_wxid
        && nodes[edge.b].u !== payload.self_wxid
      ));
      if (nodes.length === 0) {
        return {...payload, nodes, edges, empty: true};
      }
      edges.forEach(edge => {
        if (edge.direct > 0) {
          nodes[edge.a].hasDirect = true;
          nodes[edge.b].hasDirect = true;
        }
        const photos = integer(edge.photos ?? 0);
        nodes[edge.a].photos = (nodes[edge.a].photos || 0) + photos;
        nodes[edge.b].photos = (nodes[edge.b].photos || 0) + photos;
        edge.ab.forEach((value, factIndex) => {
          if (!nodes[edge.a].hasDirectionalFacts) {
            nodes[edge.a].outward[factIndex] += value;
          }
          if (!nodes[edge.b].hasDirectionalFacts) {
            nodes[edge.b].inward[factIndex] += value;
          }
        });
        edge.ba.forEach((value, factIndex) => {
          if (!nodes[edge.b].hasDirectionalFacts) {
            nodes[edge.b].outward[factIndex] += value;
          }
          if (!nodes[edge.a].hasDirectionalFacts) {
            nodes[edge.a].inward[factIndex] += value;
          }
        });
      });

      const maxWeight = Math.max(0, ...edges.map(edge => edge.w));
      const maxCombinedWeight = Math.max(0, ...edges.map(edge => edge.combined));
      const communityCounts = new Map();
      nodes.filter(node => node.deg > 0).forEach(node => {
        const community = integer(node.community);
        communityCounts.set(community, (communityCounts.get(community) || 0) + 1);
      });
      const clusterByCommunity = new Map();
      nodes.forEach((node, index) => {
        const community = integer(node.community);
        const peripheral = node.deg <= 0 || (communityCounts.get(community) || 0) < 3;
        const key = peripheral ? 'peripheral' : String(community);
        if (!clusterByCommunity.has(key)) {
          clusterByCommunity.set(key, {
            id: peripheral ? -1 : community,
            members: [],
            peripheral,
          });
        }
        clusterByCommunity.get(key).members.push(index);
      });
      const clusters = [...clusterByCommunity.values()]
        .sort((left, right) => (
          Number(left.peripheral) - Number(right.peripheral)
          || right.members.length - left.members.length
          || left.id - right.id
        ))
        .map((cluster, rank, rows) => {
          const angle = rank * Math.PI * (3 - Math.sqrt(5));
          const radius = rows.length <= 1 ? 0 : 0.12 + 0.72 * Math.sqrt(rank / (rows.length - 1));
          return {
            ...cluster,
            rank,
            targetX: radius * Math.cos(angle),
            targetY: radius * Math.sin(angle),
            label: cluster.peripheral ? '未形成密切社群' : communityNames.get(cluster.id) || `社群 ${rank + 1}`,
            color: clusterColor(cluster),
          };
        });
      clusters.forEach((cluster, clusterIndex) => {
        cluster.members.forEach(nodeIndex => {
          nodes[nodeIndex].clusterIndex = clusterIndex;
        });
      });
      edges.forEach(edge => {
        edge.crossCluster = nodes[edge.a].clusterIndex !== nodes[edge.b].clusterIndex;
      });
      clusters.forEach(cluster => {
        cluster.crossEdgeCount = 0;
        cluster.crossWeight = 0;
        cluster.isolated = false;
      });
      edges.forEach(edge => {
        if (!edge.crossCluster || edge.direct <= 0) {
          return;
        }
        const left = clusters[nodes[edge.a].clusterIndex];
        const right = clusters[nodes[edge.b].clusterIndex];
        if (left.peripheral || right.peripheral) {
          return;
        }
        left.crossEdgeCount += 1;
        right.crossEdgeCount += 1;
        left.crossWeight += edge.direct;
        right.crossWeight += edge.direct;
      });
      const connectedClusters = clusters.filter(cluster => (
        !cluster.peripheral && cluster.crossEdgeCount > 0
      ));
      const isolatedClusters = clusters.filter(cluster => (
        !cluster.peripheral && cluster.crossEdgeCount === 0
      ));
      isolatedClusters.forEach(cluster => {
        cluster.isolated = true;
        cluster.label = communityNames.get(cluster.id) || `独立社群 ${cluster.rank + 1}`;
      });
      const packedCircles = [...connectedClusters, ...isolatedClusters].map(cluster => {
        cluster.layoutRadius = 0.13 + 0.035 * Math.sqrt(cluster.members.length);
        return {
          cluster,
          r: cluster.layoutRadius + 0.14,
        };
      });
      window.d3.packSiblings(packedCircles);
      packedCircles.forEach(circle => {
        circle.cluster.targetX = circle.x;
        circle.cluster.targetY = circle.y;
        circle.cluster.packingRadius = circle.r;
      });
      let layoutMinX = Math.min(0, ...packedCircles.map(circle => circle.x - circle.r));
      let layoutMaxX = Math.max(0, ...packedCircles.map(circle => circle.x + circle.r));
      let layoutMinY = Math.min(0, ...packedCircles.map(circle => circle.y - circle.r));
      let layoutMaxY = Math.max(0, ...packedCircles.map(circle => circle.y + circle.r));
      clusters.filter(cluster => cluster.peripheral).forEach(cluster => {
        cluster.layoutRadius = 0.13 + 0.025 * Math.sqrt(cluster.members.length);
        cluster.packingRadius = cluster.layoutRadius + 0.14;
        cluster.targetX = 0;
        cluster.targetY = layoutMaxY + cluster.packingRadius + 0.2;
        layoutMinX = Math.min(layoutMinX, -cluster.packingRadius);
        layoutMaxX = Math.max(layoutMaxX, cluster.packingRadius);
        layoutMaxY = cluster.targetY + cluster.packingRadius;
      });
      const cameraCenterX = (layoutMinX + layoutMaxX) / 2 * LAYOUT_EXPANSION;
      const cameraCenterY = (layoutMinY + layoutMaxY) / 2 * LAYOUT_EXPANSION;
      const cameraWidth = Math.max(3, layoutMaxX - layoutMinX + 0.3);
      const cameraHeight = Math.max(3, layoutMaxY - layoutMinY + 0.3);
      clusters.forEach(cluster => {
        const centroid = cluster.members.reduce(
          (total, nodeIndex) => ({
            x: total.x + nodes[nodeIndex].x,
            y: total.y + nodes[nodeIndex].y,
          }),
          {x: 0, y: 0}
        );
        centroid.x /= cluster.members.length;
        centroid.y /= cluster.members.length;
        cluster.targetX *= LAYOUT_EXPANSION;
        cluster.targetY *= LAYOUT_EXPANSION;
        cluster.layoutRadius *= LAYOUT_EXPANSION;
        cluster.packingRadius *= LAYOUT_EXPANSION;
        cluster.members.forEach(nodeIndex => {
          const node = nodes[nodeIndex];
          node.x = cluster.targetX + (node.x - centroid.x) * LAYOUT_EXPANSION;
          node.y = cluster.targetY + (node.y - centroid.y) * LAYOUT_EXPANSION;
        });
      });

      const labelNodes = nodes
        .map((node, index) => ({index, degree: node.deg}))
        .sort((left, right) => right.degree - left.degree)
        .slice(0, 28)
        .map(row => row.index);
      return {
        ...payload,
        nodes,
        edges,
        maxWeight,
        maxCombinedWeight,
        displayNodeCount: nodes.filter(node => node.u !== payload.self_wxid).length,
        labelNodes,
        clusters,
        bounds: {
          centerX: cameraCenterX,
          centerY: cameraCenterY,
          width: cameraWidth,
          height: cameraHeight,
        },
      };
    };

    const showDetails = index => {
      const detailIndex = index ?? selected;
      if (detailIndex === null || !graph) {
        details.textContent = '搜索姓名或将指针移到节点上查看互动明细。';
        return;
      }
      const node = graph.nodes[detailIndex];
      const cluster = graph.clusters[node.clusterIndex];
      const element = (tag, text, className) => {
        const created = document.createElement(tag);
        if (text !== undefined) created.textContent = text;
        if (className) created.className = className;
        return created;
      };
      const heading = element('h3', nodeLabel(node));
      const swatch = element('i');
      swatch.style.background = cluster.color;
      const meta = element('p', `${cluster.label} · ${node.known ? '已匹配联系人' : '未匹配联系人'}`);
      meta.prepend(swatch);
      const rows = element('dl');
      [
        ['关系', formatNumber(node.partners)],
        ['我发出', `赞 ${formatNumber(node.outward[0])} · 评 ${formatNumber(node.outward[1])} · 回 ${formatNumber(node.outward[2])} · 提醒 ${formatNumber(node.outward[3])}`],
        ['收到', `赞 ${formatNumber(node.inward[0])} · 评 ${formatNumber(node.inward[1])} · 回 ${formatNumber(node.inward[2])} · 被提醒 ${formatNumber(node.inward[3])}`],
        ['共同受众', formatNumber(node.co)],
        ['共同群', formatNumber(node.group)],
        ...(node.photos ? [['合照', `${formatNumber(node.photos)} 张（已确认的人脸）`]] : []),
        ['最近互动', node.last || '未知'],
      ].forEach(([label, value]) => rows.append(element('dt', label), element('dd', value)));
      details.replaceChildren(heading, meta, rows);
      if (node.known && node.hash) {
        const link = element('a', `打开 ${nodeLabel(node)} 的资料`, 'graph-open');
        link.href = root.dataset.contactUrlTemplate.replace('__CONTACT_HASH__', encodeURIComponent(String(node.hash)));
        details.append(link);
      }
    };

    const nearestNode = (x, y) => {
      if (!graph) {
        return null;
      }
      let nearest = null;
      let nearestDistance = 18;
      graph.nodes.forEach((node, index) => {
        if (!graph.visibleNodeIndexes?.has(index)) {
          return;
        }
        const point = projection(node);
        const distance = Math.hypot(point.x - x, point.y - y);
        if (distance <= Math.max(nearestDistance, nodeRadius(node) + 5)) {
          nearest = index;
          nearestDistance = distance;
        }
      });
      return nearest;
    };

    const localPoint = event => {
      const rectangle = canvas.getBoundingClientRect();
      return {x: event.clientX - rectangle.left, y: event.clientY - rectangle.top};
    };

    const zoomAt = (factor, point) => {
      const oldScale = view.scale;
      const newScale = Math.max(0.005, Math.min(20, oldScale * factor));
      factor = newScale / oldScale;
      const centerX = width / 2;
      const centerY = height / 2;
      view.panX = point.x - centerX - (point.x - centerX - view.panX) * factor;
      view.panY = point.y - centerY - (point.y - centerY - view.panY) * factor;
      view.scale = newScale;
    };

    const resetView = () => {
      if (graph?.nodes.length) {
        const xs = graph.nodes.map(n => n.x), ys = graph.nodes.map(n => n.y);
        const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
        graph.bounds = {centerX:(minX+maxX)/2,centerY:(minY+maxY)/2,width:Math.max(1,maxX-minX)*1.1,height:Math.max(1,maxY-minY)*1.1};
      }
      view.scale = 1;
      view.panX = 0;
      view.panY = 0;
      render();
    };

    const startClusterDrag = (nodeIndex, point) => {
      const node = graph.nodes[nodeIndex];
      const cluster = graph.clusters[node.clusterIndex];
      const originPointer = unproject(point);
      clusterDrag = {
        cluster,
        originPointer,
        originTargetX: cluster.targetX,
        originTargetY: cluster.targetY,
        deltaX: 0,
        deltaY: 0,
        members: cluster.members.map(index => {
          const member = graph.nodes[index];
          return {
            index,
            node: member,
            x: member.x,
            y: member.y,
            fx: member.fx ?? null,
            fy: member.fy ?? null,
            pinned: member.pinned,
          };
        }),
      };
      clusterDrag.members.forEach(member => {
        member.node.fx = member.node.x;
        member.node.fy = member.node.y;
      });
      node.pinned = true;
    };

    const moveClusterDrag = point => {
      if (!clusterDrag) {
        return;
      }
      const position = unproject(point);
      const targetX = clusterDrag.originTargetX
        + position.x - clusterDrag.originPointer.x;
      const targetY = clusterDrag.originTargetY
        + position.y - clusterDrag.originPointer.y;
      clusterDrag.deltaX = targetX - clusterDrag.originTargetX;
      clusterDrag.deltaY = targetY - clusterDrag.originTargetY;
      clusterDrag.cluster.targetX = targetX;
      clusterDrag.cluster.targetY = targetY;
      clusterDrag.members.forEach(member => {
        member.node.x = member.x + clusterDrag.deltaX;
        member.node.y = member.y + clusterDrag.deltaY;
        member.node.fx = member.node.x;
        member.node.fy = member.node.y;
        member.node.vx = 0;
        member.node.vy = 0;
      });
    };

    const finishClusterDrag = commit => {
      if (!clusterDrag) {
        return;
      }
      if (!commit) {
        clusterDrag.cluster.targetX = clusterDrag.originTargetX;
        clusterDrag.cluster.targetY = clusterDrag.originTargetY;
      }
      clusterDrag.members.forEach(member => {
        if (!commit) {
          member.node.x = member.x;
          member.node.y = member.y;
          member.node.pinned = member.pinned;
          member.node.fx = member.fx;
          member.node.fy = member.fy;
          return;
        }
        const remainsPinned = member.index === draggingNode || member.pinned;
        member.node.pinned = remainsPinned;
        member.node.fx = remainsPinned ? member.node.x : null;
        member.node.fy = remainsPinned ? member.node.y : null;
      });
      clusterDrag = null;
    };

    const showFootnote = payload => {
      const coverage = payload.coverage || {};
      const known = payload.nodes.filter(node => node.known && node.hash).length;
      const posts = number(coverage.posts);
      const rawGenerated = payload.generated_at;
      const generatedValue = typeof rawGenerated === 'number'
        || (typeof rawGenerated === 'string' && /^\d+$/.test(rawGenerated))
        ? Number(rawGenerated) * 1000
        : rawGenerated;
      const generatedDate = generatedValue ? new Date(generatedValue) : null;
      const generated = generatedDate && !Number.isNaN(generatedDate.getTime())
        ? generatedDate.toLocaleString('zh-CN')
        : '未知';
      const parts = [
        `生成于 ${generated}`,
        `Monica 联系人匹配 ${formatNumber(known)} / ${formatNumber(payload.nodes.length)}`,
      ];
      if (posts > 0) {
        parts.push(`基于 ${formatNumber(posts)} 条已缓存朋友圈`);
      }
      parts.push('覆盖度受 sns.db 已缓存范围限制；图中缺少边不代表现实关系缺失');
      footnote.textContent = parts.join(' · ');
      footnote.hidden = false;
    };

    const load = async () => {
      if (loading || graph) {
        return;
      }
      loading = true;
      loadButton.disabled = true;
      setStatus('正在读取本机关系图…');
      details.textContent = '正在加载数据。';
      drawMessage('正在加载关系图…');
      try {
        const response = await fetch(root.dataset.graphSource, {
          credentials: 'same-origin',
          headers: {Accept: 'application/json'},
        });
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        const payload = await response.json();
        const prepared = prepareGraph(payload);
        showFootnote(prepared);
        if (prepared.empty) {
          simulation?.stop();
          simulation = null;
          graph = null;
          setStatus('关系图数据为空；当前快照没有可显示的节点。');
          details.textContent = '没有可显示的关系明细。';
          drawMessage('当前快照没有关系节点');
          return;
        }
        graph = prepared;
        selected = null;
        message = '';
        // The page lists these so they can be renamed; `key` is the API's stable community id.
        const keys = new Map((payload.communities || []).map(community => [integer(community.index), community]));
        root.dispatchEvent(new CustomEvent('graph-communities', {
          detail: graph.clusters.filter(cluster => !cluster.peripheral && keys.has(cluster.id)).map(cluster => ({
            id: keys.get(cluster.id).key,
            label: cluster.label,
            size: cluster.members.length,
            label_source: keys.get(cluster.id).label_source,
            color: cluster.color,
          })),
        }));
        const options = document.createDocumentFragment();
        graph.nodes
          .filter(node => node.u !== graph.self_wxid)
          .map(node => nodeLabel(node))
          .sort((left, right) => left.localeCompare(right, 'zh-CN'))
          .forEach(label => {
            const option = document.createElement('option');
            option.value = label;
            options.append(option);
          });
        searchOptions?.replaceChildren(options);
        filter.max = '100';
        filter.step = '1';
        filter.value = '0';
        filterValue.value = '0';
        threshold = 0;
        includeGroups = false;
        if (groupToggle) {
          groupToggle.checked = false;
        }
        controls.hidden = false;
        loadButton.textContent = '关系图已加载';
        showDetails(null);
        resetView();
        createSimulation();
        setSimulationRunning(false);
        const focus = new URLSearchParams(window.location.search).get('focus');
        const index = graph.nodes.findIndex(node => node.profile_id === focus || node.hash === focus);
        if (index >= 0) { selected = index; showDetails(index); render(); }
        canvas.focus({preventScroll: true});
      } catch (error) {
        simulation?.stop();
        simulation = null;
        graph = null;
        loadButton.disabled = false;
        loadButton.textContent = '重试加载';
        controls.hidden = true;
        footnote.hidden = true;
        setStatus(`关系图加载失败：${error.message}`, true);
        details.textContent = '请确认静态图文件已生成并挂载，然后重试。';
        drawMessage('关系图加载失败');
      } finally {
        loading = false;
      }
    };

    loadButton.addEventListener('click', load);
    motionButton?.addEventListener('click', () => {
      setSimulationRunning(!simulationRunning);
    });
    groupToggle?.addEventListener('change', () => {
      includeGroups = groupToggle.checked;
      const position = number(filter.value) / 100;
      threshold = activeMaximumWeight() * position ** 3;
      filterValue.value = threshold.toFixed(threshold < 10 ? 2 : 1);
      configureSimulation();
      render();
    });
    searchInput?.addEventListener('input', () => searchInput.setCustomValidity(''));
    searchForm?.addEventListener('submit', event => {
        event.preventDefault();
        if (!graph) {
          return;
        }
        const query = searchInput.value.trim().toLocaleLowerCase('zh-CN');
        const index = graph.nodes.findIndex(node => {
          if (node.u === graph.self_wxid) {
            return false;
          }
          const labels = [nodeLabel(node), node.label, node.name, node.u]
            .filter(Boolean)
            .map(value => String(value).toLocaleLowerCase('zh-CN'));
          return labels.some(label => label === query);
        });
        const fallback = index >= 0 ? index : graph.nodes.findIndex(node => (
          node.u !== graph.self_wxid
          && [nodeLabel(node), node.label, node.name, node.u]
            .filter(Boolean)
            .some(value => String(value).toLocaleLowerCase('zh-CN').includes(query))
        ));
        if (!query || fallback < 0) {
          searchInput.setCustomValidity('没有找到匹配的关系节点');
          searchInput.reportValidity();
          return;
        }
        selected = fallback;
        const node = graph.nodes[selected];
        if (!node.hasDirect && groupToggle) {
          includeGroups = true;
          groupToggle.checked = true;
        }
        view.scale = Math.max(view.scale, 1.5);
        const scale = graphScale() * view.scale;
        view.panX = -(node.x - graph.bounds.centerX) * scale;
        view.panY = -(node.y - graph.bounds.centerY) * scale;
        showDetails(selected);
        render();
        canvas.focus({preventScroll: true});
      });
    resetButton.addEventListener('click', resetView);
    root.querySelector('[data-graph-zoom-in]')?.addEventListener('click',()=>{zoomAt(1.4,{x:width/2,y:height/2});render();});
    root.querySelector('[data-graph-zoom-out]')?.addEventListener('click',()=>{zoomAt(1/1.4,{x:width/2,y:height/2});render();});
    filter.addEventListener('input', () => {
      const position = number(filter.value) / 100;
      threshold = graph ? activeMaximumWeight() * position ** 3 : 0;
      filterValue.value = threshold.toFixed(threshold < 10 ? 2 : 1);
      configureSimulation();
      render();
    });

    canvas.addEventListener('wheel', event => {
      if (!graph) {
        return;
      }
      event.preventDefault();
      zoomAt(Math.exp(-event.deltaY * 0.0015), localPoint(event));
      render();
    }, {passive: false});

    canvas.addEventListener('pointerdown', event => {
      if (!graph) {
        return;
      }
      canvas.setPointerCapture(event.pointerId);
      const point = localPoint(event);
      pointers.set(event.pointerId, point);
      pointerStart = point;
      dragged = false;
      if (pointers.size === 1) {
        draggingNode = nearestNode(point.x, point.y);
        if (draggingNode !== null) {
          startClusterDrag(draggingNode, point);
          if (simulationRunning) {
            simulation.alphaTarget(0.12).restart();
          }
          showDetails(draggingNode);
          canvas.style.cursor = 'grabbing';
        }
      }
      if (pointers.size === 2) {
        finishClusterDrag(false);
        draggingNode = null;
        multiPointerGesture = true;
        const [left, right] = [...pointers.values()];
        pinch = {
          distance: Math.max(1, Math.hypot(right.x - left.x, right.y - left.y)),
          midpoint: {x: (left.x + right.x) / 2, y: (left.y + right.y) / 2},
        };
      }
    });

    canvas.addEventListener('pointermove', event => {
      const point = localPoint(event);
      if (!pointers.has(event.pointerId)) {
        const nextHovered = nearestNode(point.x, point.y);
        if (nextHovered !== hovered) {
          hovered = nextHovered;
          canvas.style.cursor = hovered === null ? 'grab' : 'pointer';
          showDetails(hovered);
          render();
        }
        return;
      }

      const previous = pointers.get(event.pointerId);
      pointers.set(event.pointerId, point);
      if (pointerStart && Math.hypot(point.x - pointerStart.x, point.y - pointerStart.y) > 4) {
        dragged = true;
      }
      if (pointers.size === 1) {
        if (draggingNode !== null) {
          moveClusterDrag(point);
        } else {
          view.panX += point.x - previous.x;
          view.panY += point.y - previous.y;
        }
      } else if (pointers.size === 2) {
        const [left, right] = [...pointers.values()];
        const distance = Math.max(1, Math.hypot(right.x - left.x, right.y - left.y));
        const midpoint = {x: (left.x + right.x) / 2, y: (left.y + right.y) / 2};
        if (pinch) {
          view.panX += midpoint.x - pinch.midpoint.x;
          view.panY += midpoint.y - pinch.midpoint.y;
          zoomAt(distance / pinch.distance, midpoint);
        }
        pinch = {distance, midpoint};
      }
      render();
    });

    const endPointer = event => {
      if (!pointers.has(event.pointerId)) {
        return;
      }
      const point = localPoint(event);
      const wasSinglePointer = pointers.size === 1;
      pointers.delete(event.pointerId);
      pinch = null;
      if (draggingNode !== null) {
        finishClusterDrag(dragged && event.type === 'pointerup');
        if (dragged && event.type === 'pointerup') {
          selected = draggingNode;
          showDetails(selected);
        }
      }
      if (simulationRunning) {
        simulation.alphaTarget(0.025);
      }
      if (wasSinglePointer && !dragged && !multiPointerGesture) {
        const index = draggingNode ?? nearestNode(point.x, point.y);
        const node = index === null ? null : graph.nodes[index];
        if (index === null) {
          selected = null;
          showDetails(null);
          render();
        } else if (selected !== index) {
          selected = index;
          showDetails(index);
          render();
        } else if (node?.known && node.hash) {
          const url = root.dataset.contactUrlTemplate.replace(
            '__CONTACT_HASH__',
            encodeURIComponent(String(node.hash))
          );
          navigationTimer = window.setTimeout(() => window.location.assign(url), 280);
        }
      }
      if (pointers.size === 0) {
        draggingNode = null;
        pointerStart = null;
        multiPointerGesture = false;
        canvas.style.cursor = hovered === null ? 'grab' : 'pointer';
      }
    };
    canvas.addEventListener('pointerup', endPointer);
    canvas.addEventListener('pointercancel', endPointer);
    canvas.addEventListener('dblclick', event => {
      if (navigationTimer !== null) {
        window.clearTimeout(navigationTimer);
        navigationTimer = null;
      }
      if (!graph) {
        return;
      }
      const index = nearestNode(localPoint(event).x, localPoint(event).y);
      if (index !== null) {
        graph.nodes[index].pinned = false;
        graph.nodes[index].fx = null;
        graph.nodes[index].fy = null;
        if (simulationRunning) {
          simulation.alpha(0.35).restart();
        }
        selected = index;
        showDetails(index);
        render();
      }
    });
    canvas.addEventListener('pointerleave', () => {
      if (pointers.size === 0 && hovered !== null) {
        hovered = null;
        showDetails(null);
        render();
      }
    });

    canvas.addEventListener('keydown', event => {
      if (!graph) {
        return;
      }
      const actions = {
        Enter: () => { details.querySelector('a')?.click(); },
        ArrowLeft: () => { view.panX += 24; },
        ArrowRight: () => { view.panX -= 24; },
        ArrowUp: () => { view.panY += 24; },
        ArrowDown: () => { view.panY -= 24; },
        '+': () => zoomAt(1.2, {x: width / 2, y: height / 2}),
        '=': () => zoomAt(1.2, {x: width / 2, y: height / 2}),
        '-': () => zoomAt(1 / 1.2, {x: width / 2, y: height / 2}),
        '0': resetView,
        Escape: () => {
          selected = null;
          showDetails(null);
        },
      };
      if (actions[event.key]) {
        event.preventDefault();
        actions[event.key]();
        render();
      }
    });

    const repaint = () => {
      palette = readPalette(root);
      graph?.clusters.forEach(cluster => { cluster.color = clusterColor(cluster); });
      render();
    };
    const themeObserver = new MutationObserver(repaint);
    themeObserver.observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
    const scheme = window.matchMedia('(prefers-color-scheme: dark)');
    scheme.addEventListener('change', repaint);

    if ('ResizeObserver' in window) {
      const ro = new ResizeObserver(resize); ro.observe(canvas);
      root._destroyGraph = () => {
        simulation?.stop(); ro.disconnect(); themeObserver.disconnect(); scheme.removeEventListener('change', repaint);
      };
    } else {
      window.addEventListener('resize', resize);
    }
    if ('IntersectionObserver' in window) {
      const observer = new IntersectionObserver(entries => {
        if (entries.some(entry => entry.isIntersecting)) {
          observer.disconnect();
          load();
        }
      }, {rootMargin: '160px 0px', threshold: 0.1});
      observer.observe(root);
    }
    root._reloadGraph = load;
    updateMotionButton();
    resize();
  });
};
window.initMonicaGraph();
