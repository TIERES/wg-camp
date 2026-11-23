// Escolher ISO (templates/tournaments/_iso_picker.html): o campo [data-iso-choice="<id da
// janela>"] abre a janela com os cards num carrossel (as 20 cadastradas mais recentes; o
// filtro procura em todas, sem acento/maiúscula). A escolhida fica com a borda verde e em
// primeiro; clicar nela de novo tira a escolha. O valor vai no input [data-iso-value]
// (name="iso"), que dispara "iso:change" (detail.option = o card escolhido ou null).
// - data-iso-preset="c:12": já abre com ela escolhida (o formulário voltou com erro);
//   data-iso-param="iso": o mesmo pelo parâmetro da URL (?iso=c:12).
// - campo.isoChoose(valor ou null): escolhe/tira de fora (ex.: o jogo base mudou).
(function () {
  var LIMIT = 20;
  function plain(s) { return (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase(); }

  function setup(choice) {
    var dialog = document.getElementById(choice.getAttribute('data-iso-choice'));
    if (!dialog || choice.isoChoose) return;
    var value = choice.querySelector('[data-iso-value]'), chosen = choice.querySelector('[data-iso-chosen]');
    var open = choice.querySelector('[data-iso-open]'), filter = dialog.querySelector('[data-iso-filter]');
    var empty = dialog.querySelector('[data-iso-empty]'), shownNote = dialog.querySelector('[data-iso-shown]');
    var carousel = dialog.querySelector('[data-carousel]');
    var track = carousel ? carousel.querySelector('[data-carousel-track]') : null;
    var options = Array.prototype.slice.call(dialog.querySelectorAll('.iso-option'));
    var openLabel = open.textContent, selected = null;

    function render() {
      var words = plain(filter.value).split(/\s+/).filter(Boolean), shown = 0, found = 0;
      var ordered = options.slice().sort(function (a, b) {
        return (b === selected) - (a === selected) || a.dataset.order - b.dataset.order;
      });
      ordered.forEach(function (o) {
        var text = plain(o.dataset.search);
        var match = words.every(function (w) { return text.indexOf(w) >= 0; });
        if (match) found++;
        o.hidden = !match || (!words.length && shown >= LIMIT);
        if (!o.hidden) shown++;
        o.classList.toggle('is-selected', o === selected);
        o.setAttribute('aria-pressed', o === selected ? 'true' : 'false');
        o.title = o === selected ? 'Clique para tirar a escolha desta ISO' : 'Escolher esta ISO';
        if (track) track.appendChild(o);
      });
      empty.hidden = shown > 0;
      if (shownNote) {
        shownNote.textContent = words.length ? (found ? found + (found === 1 ? ' ISO encontrada' : ' ISOs encontradas') : '')
          : (found > LIMIT ? 'As ' + LIMIT + ' ISOs cadastradas mais recentes (de ' + found + '): use o filtro para achar as outras.'
                           : 'ISOs cadastradas, das mais recentes para as mais antigas.');
      }
      if (carousel) carousel.dispatchEvent(new Event('carousel:refresh'));
    }
    function show(option) {
      var changed = option !== selected;
      selected = option;
      value.value = option ? option.dataset.value : '';
      chosen.innerHTML = '';
      if (option) {
        var copy = option.cloneNode(true);
        copy.removeAttribute('data-value');
        copy.removeAttribute('title');
        copy.removeAttribute('aria-pressed');
        copy.classList.remove('is-selected');
        copy.classList.add('iso-chosen-card');
        copy.hidden = false;
        copy.addEventListener('click', function () { open.click(); });
        chosen.appendChild(copy);
      }
      chosen.hidden = !option;
      open.textContent = option ? 'Trocar ISO' : openLabel;
      if (changed) value.dispatchEvent(new CustomEvent('iso:change', { bubbles: true, detail: { option: option } }));
    }
    function find(wanted) {
      return wanted ? options.filter(function (o) { return o.dataset.value === wanted; })[0] || null : null;
    }
    open.addEventListener('click', function () {
      filter.value = '';
      dialog.showModal();
      render();
      filter.focus();
    });
    filter.addEventListener('keydown', function (e) { if (e.key === 'Enter') e.preventDefault(); });
    filter.addEventListener('input', render);
    options.forEach(function (o) {
      o.addEventListener('click', function () {
        if (o === selected) { show(null); render(); return; }  // tira a escolha; a janela fica aberta
        show(o);
        dialog.close();
      });
    });
    // Fecha clicando fora do conteúdo.
    dialog.addEventListener('click', function (e) { if (e.target === dialog) dialog.close(); });
    choice.isoChoose = function (wanted) { show(find(wanted)); };
    var param = choice.getAttribute('data-iso-param');
    var preset = find(choice.getAttribute('data-iso-preset') ||
                      (param ? new URLSearchParams(location.search).get(param) : null));
    if (preset) show(preset);
  }

  function setupAll() { document.querySelectorAll('[data-iso-choice]').forEach(setup); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setupAll);
  else setupAll();
})();
