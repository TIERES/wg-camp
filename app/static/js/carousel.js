// Carrossel - o formato padrão das listas de cards (templates/_carousel.html e o
// friendly_carousel de tournaments/_friendly.html). Dentro de [data-carousel]:
// a faixa com rolagem lateral [data-carousel-track] (cada filho é um card), as setas
// nas laterais ([data-carousel-prev] / [data-carousel-next]) e a contagem
// [data-carousel-count] no alto à direita. Com todos os cards na tela, setas e
// contagem somem. Quantos cards por tela vem do CSS (largura de cada card).
// - data-carousel="fit": a faixa na altura dos cards da vez (um card curto não
//   deixa o espaço vazio do card mais alto).
// - Cards com hidden ficam de fora (filtro). Os cards podem ser trocados (Ao vivo
//   recarrega a cada 10 s): a lista se atualiza e fica onde estava.
// - O evento "carousel:refresh" no carrossel recalcula e volta para o começo.
// - window.setupCarousels(raiz): liga os carrosséis de um trecho trocado da página.
(function () {
  function setup(box) {
    if (box.dataset.carouselReady) return;
    var track = box.querySelector('[data-carousel-track]');
    if (!track) return;
    box.dataset.carouselReady = '1';
    var prev = box.querySelector('[data-carousel-prev]'), next = box.querySelector('[data-carousel-next]');
    var count = box.querySelector('[data-carousel-count]');
    var fit = box.getAttribute('data-carousel') === 'fit', frame = 0;

    function slides() {
      return Array.prototype.filter.call(track.children, function (s) { return !s.hidden; });
    }
    // Onde está a faixa: largura de um passo (card + espaço), cards por tela e o primeiro visível.
    function state() {
      var list = slides(), step = track.clientWidth, width = step;
      if (list.length) width = list[0].offsetWidth;
      if (list.length > 1) step = list[1].offsetLeft - list[0].offsetLeft;
      else if (list.length) step = width;
      step = step || 1;
      var per = Math.max(1, Math.floor((track.clientWidth + step - width + 2) / step));
      var first = Math.min(Math.max(0, Math.round(track.scrollLeft / step)), Math.max(0, list.length - per));
      return { list: list, step: step, per: per, first: first };
    }
    function update() {
      frame = 0;
      var s = state(), n = s.list.length;
      [prev, next, count].forEach(function (el) { if (el) el.hidden = n <= s.per; });
      if (count) {
        var last = Math.min(n, s.first + s.per);
        count.textContent = n ? (s.per > 1 ? (s.first + 1) + '–' + last : String(s.first + 1)) + ' de ' + n : '';
      }
      if (prev) prev.disabled = track.scrollLeft <= 1;
      if (next) next.disabled = track.scrollLeft + track.clientWidth >= track.scrollWidth - 1;
      // Setas: altura fixa do CSS (--carousel-arrow-top), no máximo a metade do card
      // mais baixo da lista inteira - não só dos da vez, para não mexer ao navegar.
      var shortest = 0;
      s.list.forEach(function (el) { var h = el.offsetHeight; if (h && (!shortest || h < shortest)) shortest = h; });
      box.style.setProperty('--carousel-arrow-max', shortest ? Math.round(shortest / 2) + 'px' : '');
      if (fit) {
        var tallest = 0;
        s.list.slice(s.first, s.first + s.per).forEach(function (el) { tallest = Math.max(tallest, el.offsetHeight); });
        track.style.height = tallest ? tallest + 'px' : '';
      }
    }
    function later() { if (!frame) frame = requestAnimationFrame(update); }
    function go(dir) {
      var s = state();
      var target = Math.min(Math.max(0, s.first + dir * s.per), Math.max(0, s.list.length - 1));
      track.scrollTo({ left: s.list[target] ? s.list[target].offsetLeft - s.list[0].offsetLeft : 0, behavior: 'smooth' });
    }
    if (prev) prev.addEventListener('click', function () { go(-1); });
    if (next) next.addEventListener('click', function () { go(1); });
    track.addEventListener('scroll', later, { passive: true });
    window.addEventListener('resize', later);
    box.addEventListener('carousel:refresh', function () { track.scrollLeft = 0; update(); });
    // Altura dos cards muda (imagens carregando, placar novo) e cards trocados: acompanha.
    var sizes = window.ResizeObserver ? new ResizeObserver(later) : null;
    function watch() {
      if (!sizes) return;
      Array.prototype.forEach.call(track.children, function (s) { sizes.observe(s); });
    }
    if (sizes) { sizes.observe(track); watch(); }
    if (window.MutationObserver) new MutationObserver(function () { watch(); later(); }).observe(track, { childList: true });
    update();
  }
  window.setupCarousels = function (root) {
    (root || document).querySelectorAll('[data-carousel]').forEach(setup);
  };
  window.setupCarousels(document);
})();
