// Balão dos gráficos (evolução do Histórico do jogador e extrato da Carteira):
// placeChartTip(box, tip, point) põe o balão com a seta (.evo-tip-arrow) apontando
// para a bolinha, sem cobri-la - em cima dela quando cabe dentro do gráfico; senão,
// embaixo (classe "below": a seta vira para cima); sem caber em nenhum dos dois,
// no lado com mais espaço. --arrow-x = onde a bolinha fica dentro do balão.
(function () {
  var GAP = 12;
  window.placeChartTip = function (box, tip, point) {
    var b = box.getBoundingClientRect(), r = point.getBoundingClientRect();
    var x = r.left - b.left + r.width / 2;
    var left = Math.min(Math.max(x - tip.offsetWidth / 2, 4), b.width - tip.offsetWidth - 4);
    var above = r.top - b.top - tip.offsetHeight - GAP, below = r.bottom - b.top + GAP;
    var under = above < 0 && b.bottom - r.bottom > r.top - b.top;
    tip.classList.toggle('below', under);
    tip.style.left = left + 'px';
    tip.style.top = (under ? below : above) + 'px';
    tip.style.setProperty('--arrow-x', Math.min(Math.max(x - left, 12), tip.offsetWidth - 12) + 'px');
  };
})();
