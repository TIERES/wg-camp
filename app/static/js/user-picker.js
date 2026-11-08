// Escolha de usuário pelo nick - o padrão do sistema (templates/_user_picker.html,
// [data-user-picker]): campo de busca com a lista embaixo (foto, nick e rank).
// Filtra sem acento/maiúscula, com quem começa pelo texto primeiro; setas escolhem,
// Enter confirma, Esc fecha. Escolher alguém dispara "change" no campo.
// - "strict": o id vai no campo escondido [data-user-id] e só um nick da lista (ou o
//   nick exato digitado) deixa enviar o formulário. O primeiro da lista já vem marcado.
// - "free": o texto é o que vai no formulário (filtros: um trecho do nick vale);
//   a lista só sugere, e Enter sem escolher nada na lista envia o que foi digitado.
// - data-exclude="<id de outro campo>": o nick escolhido lá sai desta lista.
(function () {
  function plain(s) { return (s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase(); }

  function setup(box) {
    var strict = box.getAttribute('data-user-picker') === 'strict';
    var input = box.querySelector('[role=combobox]'), hidden = box.querySelector('[data-user-id]');
    var list = box.querySelector('[role=listbox]'), empty = list.querySelector('.user-options-empty');
    var other = box.dataset.exclude ? document.getElementById(box.dataset.exclude) : null;
    var options = Array.prototype.slice.call(list.querySelectorAll('[role=option]'));
    var ordered = options.slice(), active = -1;  // ordered: a ordem da lista na tela
    if (!options.length) return;  // ninguém para sugerir: fica um campo de texto comum

    function visible() { return ordered.filter(function (o) { return !o.hidden; }); }
    function setActive(index) {
      var shown = visible();
      options.forEach(function (o) { o.classList.remove('active'); o.setAttribute('aria-selected', 'false'); });
      active = index >= 0 && shown[index] ? index : -1;
      if (active < 0) { input.removeAttribute('aria-activedescendant'); return; }
      shown[active].classList.add('active');
      shown[active].setAttribute('aria-selected', 'true');
      input.setAttribute('aria-activedescendant', shown[active].id);
      shown[active].scrollIntoView({ block: 'nearest' });
    }
    function open() { list.hidden = false; input.setAttribute('aria-expanded', 'true'); }
    function close() { list.hidden = true; input.setAttribute('aria-expanded', 'false'); setActive(-1); }
    // Nick com o trecho digitado em destaque.
    function label(option, query) {
      var nick = option.dataset.nick, at = query ? plain(nick).indexOf(query) : -1;
      var span = option.querySelector('.user-option-name');
      span.textContent = '';
      if (at < 0) { span.textContent = nick; return; }
      var mark = document.createElement('mark');
      mark.textContent = nick.slice(at, at + query.length);
      span.append(nick.slice(0, at), mark, nick.slice(at + query.length));
    }
    function filter() {
      var query = plain(input.value.trim()), taken = other ? plain(other.value.trim()) : '';
      options.forEach(function (o) {
        var nick = plain(o.dataset.nick), at = nick.indexOf(query);
        o.hidden = at < 0 || (taken && nick === taken);
        o.dataset.rank = at === 0 ? 0 : 1;
        label(o, query);
      });
      ordered = options.slice().sort(function (a, b) {
        return a.dataset.rank - b.dataset.rank || a.dataset.order - b.dataset.order;
      });
      ordered.forEach(function (o) { list.insertBefore(o, empty); });
      empty.hidden = visible().length > 0;
      setActive(strict && query && visible().length ? 0 : -1);
    }
    // strict: o id só fica preenchido com o nick exato de alguém da lista.
    function sync() {
      if (!strict) return;
      var typed = input.value.trim().toLowerCase();
      var exact = options.filter(function (o) { return o.dataset.nick.toLowerCase() === typed; })[0];
      hidden.value = exact ? exact.dataset.id : '';
      if (exact) input.value = exact.dataset.nick;
      input.setCustomValidity(hidden.value || !typed ? '' : 'Escolha um jogador da lista.');
    }
    function choose(option) {
      input.value = option.dataset.nick;
      if (hidden) hidden.value = option.dataset.id;
      input.setCustomValidity('');
      close();
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }

    input.addEventListener('focus', function () { filter(); open(); });
    input.addEventListener('input', function () {
      if (hidden) hidden.value = '';
      input.setCustomValidity('');
      filter();
      open();
    });
    input.addEventListener('keydown', function (e) {
      var shown = visible();
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (list.hidden) { filter(); open(); }
        setActive(Math.min(active + 1, shown.length - 1));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActive(Math.max(active - 1, 0));
      } else if (e.key === 'Enter' && !list.hidden && shown[active]) {
        e.preventDefault();
        choose(shown[active]);
      } else if (e.key === 'Escape' && !list.hidden) {
        e.preventDefault();
        close();
      }
    });
    input.addEventListener('blur', function () { close(); sync(); });
    // Clicar na lista não tira o foco do campo (o blur fecharia antes do clique).
    list.addEventListener('mousedown', function (e) { e.preventDefault(); });
    list.addEventListener('click', function (e) {
      var option = e.target.closest('[role=option]');
      if (option) choose(option);
    });
    if (strict && input.form) {
      input.form.addEventListener('submit', function (e) {
        sync();
        if (!hidden.value) {
          e.preventDefault();
          input.setCustomValidity('Escolha um jogador da lista.');
          input.reportValidity();
        }
      });
      if (input.value) sync();
    }
  }
  document.querySelectorAll('[data-user-picker]').forEach(setup);
})();
