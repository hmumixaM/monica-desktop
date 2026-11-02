(() => {
  'use strict';

  const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const WIDTH = 900;
  const HEIGHT = 700;
  const CENTER_X = WIDTH / 2;
  const CENTER_Y = HEIGHT / 2;

  const number = value => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  };

  const formatNumber = value => Math.round(number(value)).toLocaleString('zh-CN');
  const directionalWeight = counts => (
    number(counts?.[0])
    + 3 * number(counts?.[1])
    + 4 * number(counts?.[2])
    + 12 * number(counts?.[3])
  );

  const initialize = root => {
    if (root.dataset.momentsD3Ready === 'true') {
      return;
    }
    root.dataset.momentsD3Ready = 'true';

    const dataElement = root.querySelector('[data-moments-graph-data]');
    const svgElement = root.querySelector('[data-moments-graph-svg]');
    const details = root.querySelector('[data-moments-graph-details]');
    const count = root.querySelector('[data-moments-visible-count]');
    const groupToggle = root.querySelector('[data-moments-group-toggle]');
    const filter = root.querySelector('[data-moments-filter]');
    const filterValue = root.querySelector('[data-moments-filter-value]');
    const resetButton = root.querySelector('[data-moments-reset]');
    if (!window.d3 || !dataElement || !svgElement) {
      if (details) {
        details.textContent = 'D3.js 未加载，关系明细仍可在下方查看。';
      }
      return;
    }

    // Colours come from the page palette (tokens.css), so light and dark themes both read well.
    const style = getComputedStyle(root);
    const token = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
    const color = {
      center: token('--color-primary', '#1b2230'),
      onCenter: token('--color-on-primary', '#ffffff'),
      surface: token('--color-surface', '#ffffff'),
      muted: token('--color-text-faint', '#8b93a1'),
      label: token('--color-text', '#1b2230'),
      labelSoft: token('--color-text-soft', '#545d6b'),
      group: token('--graph-group', '#5b7fb8'),
      reciprocal: token('--graph-reciprocal', '#2f9e6e'),
      outward: token('--graph-outward', '#d9822b'),
      inward: token('--graph-inward', '#7c5cc4'),
      neutral: token('--graph-neutral', '#4a8fa8'),
      selected: token('--color-accent', '#f5b83d'),
    };

    const payload = JSON.parse(dataElement.textContent);
    const center = {
      ...payload.center,
      center: true,
      x: CENTER_X,
      y: CENTER_Y,
      fx: CENTER_X,
      fy: CENTER_Y,
    };
    const storedNodes = new Map();
    payload.neighbors.forEach((neighbor, index) => {
      storedNodes.set(neighbor.id, {
        ...neighbor,
        center: false,
        initialX: CENTER_X + number(neighbor.x) * 400,
        initialY: CENTER_Y + number(neighbor.y) * 330,
        x: CENTER_X + number(neighbor.x) * 400,
        y: CENTER_Y + number(neighbor.y) * 330,
        rank: index,
      });
    });

    const svg = window.d3.select(svgElement)
      .attr('viewBox', `0 0 ${WIDTH} ${HEIGHT}`)
      .attr('preserveAspectRatio', 'xMidYMid meet');
    const viewport = svg.append('g');
    const zoom = window.d3.zoom()
      .scaleExtent([0.08, 8])
      .on('zoom', event => viewport.attr('transform', event.transform));
    svg.call(zoom);

    let simulation = null;
    let includeGroups = false;
    let selectedNodeId = null;
    let threshold = 0;

    const activeMaximumWeight = () => Math.max(
      number(includeGroups ? payload.maximumWeight : payload.maximumDirect),
      0.001
    );

    const relationshipWeight = neighbor => (
      number(includeGroups ? neighbor.weight : neighbor.direct)
    );

    const updateThreshold = () => {
      const position = number(filter?.value) / 100;
      threshold = activeMaximumWeight() * position ** 3;
      if (filterValue) {
        filterValue.value = threshold.toFixed(threshold < 10 ? 2 : 1);
      }
    };

    const nodeRadius = node => {
      if (node.center) {
        return 31;
      }
      if (node.groupOnly) {
        return 6;
      }
      return 7 + 6 * Math.sqrt(number(node.direct) / Math.max(number(payload.maximumDirect), 0.001));
    };

    const nodeColor = node => {
      if (node.center) {
        return color.center;
      }
      if (node.groupOnly) {
        return color.surface;
      }
      if (!node.url) {
        return color.muted;
      }
      const outward = directionalWeight(node.out);
      const inward = directionalWeight(node.in);
      if (number(node.reciprocity) >= 0.6) {
        return color.reciprocal;
      }
      if (outward > inward * 1.35) {
        return color.outward;
      }
      if (inward > outward * 1.35) {
        return color.inward;
      }
      return color.neutral;
    };

    const labelVisible = node => (
      node.center || node.rank < 12 || node.id === selectedNodeId
    );

    const showDetails = node => {
      if (!details) {
        return;
      }
      if (!node || node.center) {
        details.textContent = '将指针移到节点上查看互动明细；拖动节点固定位置，双击释放。';
        return;
      }
      const outward = node.out.reduce((total, value) => total + number(value), 0);
      const inward = node.in.reduce((total, value) => total + number(value), 0);
      details.textContent = node.groupOnly
        ? `${node.label} · 仅共同群 ${formatNumber(node.group)} · 最弱虚线关系`
        : [
            node.label,
            `赞 ${formatNumber(node.like)}`,
            `评论 ${formatNumber(node.comment)}`,
            `回复 ${formatNumber(node.reply)}`,
            `提醒 ${formatNumber(node.mention)}`,
            `去 ${formatNumber(outward)}`,
            `来 ${formatNumber(inward)}`,
            `互惠 ${formatNumber(number(node.reciprocity) * 100)}%`,
            `共同群 ${formatNumber(node.group)}`,
            `最近 ${node.last || '未知'}`,
          ].join(' · ');
    };

    const updatePositions = (linkSelection, nodeSelection) => {
      linkSelection
        .attr('x1', link => link.source.x)
        .attr('y1', link => link.source.y)
        .attr('x2', link => link.target.x)
        .attr('y2', link => link.target.y);
      nodeSelection.attr('transform', node => `translate(${node.x},${node.y})`);
    };

    const render = () => {
      simulation?.stop();
      viewport.selectAll('*').remove();

      const neighbors = payload.neighbors
        .filter(neighbor => includeGroups || !neighbor.groupOnly)
        .filter(neighbor => (
          relationshipWeight(neighbor) > 0
          && relationshipWeight(neighbor) >= threshold
        ))
        .map(neighbor => storedNodes.get(neighbor.id));
      if (!neighbors.some(node => node.id === selectedNodeId)) {
        selectedNodeId = null;
      }
      const nodes = [center, ...neighbors];
      const links = neighbors.map(neighbor => ({
        source: center.id,
        target: neighbor.id,
        groupOnly: neighbor.groupOnly,
        direct: number(neighbor.direct),
        weight: relationshipWeight(neighbor),
      }));
      const maximum = activeMaximumWeight();
      const maximumDirect = Math.max(number(payload.maximumDirect), 0.001);

      const linkSelection = viewport.append('g')
        .attr('aria-hidden', 'true')
        .selectAll('line')
        .data(links)
        .join('line')
        .attr('stroke', link => link.groupOnly ? color.group : color.muted)
        .attr('stroke-opacity', link => link.groupOnly ? 0.45 : 0.62)
        .attr('stroke-width', link => (
          link.groupOnly ? 1.25 : 1.2 + 6 * Math.sqrt(link.weight / maximum)
        ))
        .attr('stroke-dasharray', link => link.groupOnly ? '5 6' : null)
        .attr('stroke-linecap', 'round');

      const nodeSelection = viewport.append('g')
        .selectAll('a')
        .data(nodes, node => node.id)
        .join('a')
        .attr('href', node => node.url || null)
        .attr('aria-label', node => (
          node.url ? `选择联系人 ${node.label}；再次点击打开` : null
        ))
        .style('cursor', node => node.center ? 'default' : (node.url ? 'pointer' : 'grab'))
        .on('pointerenter focus', (event, node) => {
          showDetails(node);
          window.d3.select(event.currentTarget).selectAll('text').attr('display', null);
        })
        .on('pointerleave blur', (event, node) => {
          showDetails(storedNodes.get(selectedNodeId));
          window.d3.select(event.currentTarget).selectAll('text')
            .attr('display', labelVisible(node) ? null : 'none');
        })
        .on('dblclick', (event, node) => {
          if (node.center) {
            return;
          }
          event.preventDefault();
          event.stopPropagation();
          node.fx = null;
          node.fy = null;
          simulation.alpha(0.45).restart();
        });

      nodeSelection.append('circle')
        .attr('r', nodeRadius)
        .attr('fill', nodeColor)
        .attr('stroke', node => node.groupOnly ? color.group : color.surface)
        .attr('stroke-width', node => node.center ? 3 : 2);

      nodeSelection.filter(node => node.center).append('text')
        .attr('text-anchor', 'middle')
        .attr('dominant-baseline', 'central')
        .attr('fill', color.onCenter)
        .attr('font-size', '12')
        .attr('font-weight', '600')
        .text(node => node.label);

      nodeSelection.filter(node => !node.center).append('text')
        .attr('x', node => nodeRadius(node) + 5)
        .attr('y', -3)
        .attr('fill', color.label)
        .attr('font-size', '11')
        .attr('font-weight', node => node.rank < 16 ? '600' : '400')
        .attr('display', node => labelVisible(node) ? null : 'none')
        .text(node => node.label);

      nodeSelection.filter(node => !node.center && !node.groupOnly).append('text')
        .attr('x', node => nodeRadius(node) + 5)
        .attr('y', 10)
        .attr('fill', color.labelSoft)
        .attr('font-size', '9')
        .attr('display', node => labelVisible(node) ? null : 'none')
        .text(node => {
          const outward = node.out.reduce((total, value) => total + number(value), 0);
          const inward = node.in.reduce((total, value) => total + number(value), 0);
          return `去 ${formatNumber(outward)} · 来 ${formatNumber(inward)}`;
        });

      nodeSelection.append('title').text(node => {
        if (node.center) {
          return `${node.label} 的朋友圈关系`;
        }
        return node.groupOnly
          ? `${node.label}：共同群 ${formatNumber(node.group)}`
          : `${node.label}：赞 ${formatNumber(node.like)}，评论 ${formatNumber(node.comment)}，回复 ${formatNumber(node.reply)}，提醒 ${formatNumber(node.mention)}`;
      });

      const linkTargetId = link => (
        typeof link.target === 'object' ? link.target.id : link.target
      );
      const applySelection = () => {
        const active = selectedNodeId !== null;
        linkSelection
          .attr('stroke', link => (
            active && linkTargetId(link) === selectedNodeId
              ? color.selected
              : (link.groupOnly ? color.group : color.muted)
          ))
          .attr('stroke-opacity', link => (
            active ? (linkTargetId(link) === selectedNodeId ? 0.92 : 0.08) : (
              link.groupOnly ? 0.45 : 0.62
            )
          ));
        nodeSelection
          .attr('opacity', node => (
            !active || node.center || node.id === selectedNodeId ? 1 : 0.16
          ));
        nodeSelection.select('circle')
          .attr('stroke', node => (
            node.id === selectedNodeId
              ? color.selected
              : (node.groupOnly ? color.group : color.surface)
          ))
          .attr('stroke-width', node => (
            node.id === selectedNodeId ? 3 : (node.center ? 3 : 2)
          ));
        nodeSelection.selectAll('text')
          .attr('display', node => labelVisible(node) ? null : 'none');
      };
      nodeSelection.on('click', (event, node) => {
        if (node.center) {
          event.preventDefault();
          event.stopPropagation();
          selectedNodeId = null;
          showDetails(null);
          applySelection();
          return;
        }
        if (selectedNodeId === node.id && node.url) {
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        selectedNodeId = node.id;
        showDetails(node);
        applySelection();
      });
      svg.on('click.selection', () => {
        selectedNodeId = null;
        showDetails(null);
        applySelection();
      });

      const drag = window.d3.drag()
        .on('start', (event, node) => {
          if (node.center) {
            return;
          }
          if (!event.active && !REDUCED_MOTION) {
            simulation.alphaTarget(0.08).restart();
          }
          node.fx = node.x;
          node.fy = node.y;
        })
        .on('drag', (event, node) => {
          if (node.center) {
            return;
          }
          node.fx = event.x;
          node.fy = event.y;
          if (REDUCED_MOTION) {
            node.x = event.x;
            node.y = event.y;
            updatePositions(linkSelection, nodeSelection);
          }
        })
        .on('end', event => {
          if (!event.active && !REDUCED_MOTION) {
            simulation.alphaTarget(0);
          }
        });
      nodeSelection.call(drag);

      simulation = window.d3.forceSimulation(nodes)
        .force('link', window.d3.forceLink(links)
          .id(node => node.id)
          .distance(link => {
            if (link.groupOnly) {
              return 340;
            }
            return 170 + 150 * (1 - Math.sqrt(link.direct / maximumDirect));
          })
          .strength(link => link.groupOnly ? 0.06 : 0.26))
        .force('charge', window.d3.forceManyBody()
          .strength(node => node.center ? -900 : -260)
          .distanceMax(580))
        .force('collision', window.d3.forceCollide()
          .radius(node => nodeRadius(node) + (node.rank < 12 ? 38 : 14))
          .strength(0.9))
        .force('radial', window.d3.forceRadial(node => {
          if (node.center) {
            return 0;
          }
          if (node.groupOnly) {
            return 340;
          }
          return 170 + 170 * (1 - Math.sqrt(number(node.direct) / maximumDirect));
        }, CENTER_X, CENTER_Y).strength(node => node.center ? 1 : 0.34))
        .force('center', window.d3.forceCenter(CENTER_X, CENTER_Y))
        .alphaDecay(0.04)
        .velocityDecay(0.38)
        .on('tick', () => updatePositions(linkSelection, nodeSelection));
      applySelection();

      if (REDUCED_MOTION) {
        simulation.tick(180);
        simulation.stop();
        updatePositions(linkSelection, nodeSelection);
      }

      if (count) {
        count.textContent = neighbors.length.toLocaleString('zh-CN');
      }
      root.querySelectorAll('[data-moments-group-only-detail]').forEach(element => {
        element.hidden = !includeGroups;
      });
      showDetails(storedNodes.get(selectedNodeId));
    };

    groupToggle?.addEventListener('change', () => {
      includeGroups = groupToggle.checked;
      updateThreshold();
      render();
    });
    filter?.addEventListener('input', () => {
      updateThreshold();
      render();
    });
    resetButton?.addEventListener('click', () => {
      selectedNodeId = null;
      storedNodes.forEach(node => {
        node.x = node.initialX;
        node.y = node.initialY;
        node.fx = null;
        node.fy = null;
        node.vx = 0;
        node.vy = 0;
      });
      svg.transition().duration(REDUCED_MOTION ? 0 : 180).call(zoom.transform, window.d3.zoomIdentity);
      render();
    });
    root._destroyGraph = () => { simulation?.stop(); svg.on('.zoom', null); };
    root.querySelector('[data-moments-zoom-in]')?.addEventListener('click', () => svg.call(zoom.scaleBy, 1.4));
    root.querySelector('[data-moments-zoom-out]')?.addEventListener('click', () => svg.call(zoom.scaleBy, 1/1.4));
    const motion = root.querySelector('[data-moments-motion]');
    motion?.addEventListener('click', () => {
      const active = motion.getAttribute('aria-pressed') === 'true';
      if(active)simulation?.stop(); else simulation?.alpha(.4).restart();
      motion.setAttribute('aria-pressed', String(!active));
      motion.textContent = active ? '继续运动' : '暂停运动';
    });
    updateThreshold();
    render();
  };

  const scan = root => {
    if (root.matches?.('[data-moments-d3-graph]')) {
      initialize(root);
    }
    root.querySelectorAll?.('[data-moments-d3-graph]').forEach(initialize);
  };
  scan(document);
  document.addEventListener('wechat:lazy-loaded', event => {
    scan(event.detail?.root || document);
  });
})();
