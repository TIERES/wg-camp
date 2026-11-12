// Autocompletar de texto: <div class="ac"><input data-options='["..."]' aria-controls="<id>">
// <ul class="ac-list" id="<id>"></ul></div>. Sugere os itens da lista que contêm o texto
// digitado, sem diferenciar acento/maiúscula (até 8). Escolher um item (clique, ou setas +
// Enter) preenche o campo e dispara "change" - quem usa decide o que fazer (filtrar na hora,
// esperar o botão Filtrar...). Usado nos filtros de Campeonatos e do Histórico de partidas.
(function () {
  function plain(s) { return (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim(); }

  function setup(input) {
    if (input.dataset.acReady) return;
    input.dataset.acReady = '1';
    var options = JSON.parse(input.dataset.options || '[]');
    var list = document.getElementById(input.getAttribute('aria-controls'));
    var active = -1;

    function close() { list.hidden = true; input.setAttribute('aria-expanded', 'false'); active = -1; }
    function choose(value) {
      input.value = value;
      close();
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
    function mark(index) {
      var items = list.children;
      if (!items.length) return;
      active = (index + items.length) % items.length;
      for (var i = 0; i < items.length; i++) items[i].setAttribute('aria-selected', i === active ? 'true' : 'false');
      items[active].scrollIntoView({ block: 'nearest' });
    }
    function open() {
      var q = plain(input.value);
      list.innerHTML = '';
      options.filter(function (o) { var p = plain(o); return p.indexOf(q) >= 0 && p !== q; }).slice(0, 8).forEach(function (o) {
        var li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.textContent = o;
        li.addEventListener('mousedown', function (e) { e.preventDefault(); choose(o); });
        list.appendChild(li);
      });
      active = -1;
      list.hidden = !list.children.length;
      input.setAttribute('aria-expanded', list.hidden ? 'false' : 'true');
    }

    input.addEventListener('input', open);
    input.addEventListener('focus', open);
    input.addEventListener('blur', close);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (list.hidden) open();
        mark(active + (e.key === 'ArrowDown' ? 1 : -1));
      } else if (e.key === 'Enter' && !list.hidden && active >= 0) {
        e.preventDefault();
        choose(list.children[active].textContent);
      } else if (e.key === 'Escape') {
        close();
      }
    });
  }

  document.querySelectorAll('.ac input[data-options]').forEach(setup);
})();
