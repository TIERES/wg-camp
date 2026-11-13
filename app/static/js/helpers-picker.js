// Ajudantes (templates/_helpers_picker.html, [data-helpers-picker]): os amigos que ajudam a
// editar uma ISO ou um campeonato. O amigo escolhido na busca (user-picker.js) vira uma
// etiqueta com o × para tirar; os ids vão no formulário em helper_ids. Quem já foi
// escolhido sai da lista da busca (data-taken, que o user-picker.js esconde).
//   elemento.helpersSet([{id, username}]) troca os escolhidos (janela "Editar ISO").
(function () {
  function setup(box) {
    var input = box.querySelector('[role=combobox]'), chips = box.querySelector('[data-helper-chips]');
    var max = Number(box.dataset.max) || 10;
    var placeholder = input ? input.placeholder : '';

    function options() { return Array.prototype.slice.call(box.querySelectorAll('[role=option]')); }
    function chosen() { return Array.prototype.slice.call(chips.querySelectorAll('[data-id]')); }
    function refresh() {
      var ids = {};
      chosen().forEach(function (chip) { ids[chip.dataset.id] = true; });
      options().forEach(function (o) {
        if (ids[o.dataset.id]) o.setAttribute('data-taken', ''); else o.removeAttribute('data-taken');
      });
      var count = chosen().length;
      chips.hidden = !count;
      if (input) {
        input.disabled = count >= max;
        input.placeholder = count >= max ? 'No máximo ' + max + ' ajudantes' : placeholder;
      }
    }
    function add(id, nick) {
      if (chips.querySelector('[data-id="' + id + '"]') || chosen().length >= max) return;
      var chip = document.createElement('li'), name = document.createElement('span');
      var hidden = document.createElement('input'), remove = document.createElement('button');
      chip.className = 'helper-chip';
      chip.dataset.id = id;
      name.textContent = nick;
      hidden.type = 'hidden'; hidden.name = 'helper_ids'; hidden.value = id;
      remove.type = 'button'; remove.className = 'helper-remove'; remove.textContent = '×';
      remove.title = 'Tirar ' + nick;
      remove.setAttribute('aria-label', 'Tirar ' + nick);
      chip.append(name, hidden, remove);
      chips.appendChild(chip);
    }
    // O nick digitado (ou escolhido na lista) é de alguém que pode ser ajudante?
    function take() {
      if (!input) return;
      var typed = input.value.trim().toLowerCase();
      if (!typed) return;
      var found = options().filter(function (o) {
        return !o.hasAttribute('data-taken') && o.dataset.nick.toLowerCase() === typed;
      })[0];
      if (!found) return;
      add(found.dataset.id, found.dataset.nick);
      input.value = '';
      refresh();
      // A lista da busca (user-picker.js) se refaz sem o escolhido e fecha: ela cobre as etiquetas.
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    }

    chips.addEventListener('click', function (e) {
      var button = e.target.closest('.helper-remove');
      if (!button) return;
      button.parentNode.remove();
      refresh();
      if (input) input.focus();
    });
    if (input) {
      input.addEventListener('change', take);  // o user-picker.js avisa a escolha com "change"
      // Enter nunca envia o formulário daqui: escolhe o nick digitado, se for de um amigo.
      input.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        take();
      });
    }
    box.helpersSet = function (list) {
      chips.textContent = '';
      (list || []).forEach(function (p) { add(String(p.id), p.username); });
      if (input) input.value = '';
      refresh();
    };
    refresh();
  }
  document.querySelectorAll('[data-helpers-picker]').forEach(setup);
})();
