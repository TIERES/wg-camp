/* Times dos inscritos, em Gerenciar > Participantes (manage.html #inscritos): o organizador arrasta os
   times para as vagas dos inscritos, antes do início. Cada inscrito leva um <input type=hidden
   name=time_ID> com o time da vaga ("e<inscrição>": o time que essa inscrição tinha; "t<time>": um time
   da lista; vazio: sem time), que vai no "Salvar times" (tournaments.assign_teams).

   - Soltar um time na vaga de outro inscrito troca os dois de lugar; soltar um time da lista numa vaga
     ocupada devolve o time que estava nela para a lista. Soltar na lista ("Times sem dono", só nos
     modos com lista) deixa o inscrito sem time.
   - Mouse: arrasta pelo cartão inteiro. Toque: pela alça (⠿), para o resto da tela continuar rolando.
     Perto da borda da tela, a página rola.
   - Teclado: num time de um inscrito, ↑ e ↓ trocam com o inscrito de cima/de baixo e Delete devolve
     para a lista; num time da lista, Enter leva ao primeiro inscrito sem time. */
(function () {
  var board = document.getElementById('team-board');
  if (!board) return;
  var form = board.closest('form');
  var save = form.querySelector('[data-save]'), undo = form.querySelector('[data-undo]');
  var dirtyNote = form.querySelector('[data-dirty]');
  var pool = board.querySelector('[data-pool]'), poolChips = pool && pool.querySelector('.team-pool-chips');
  var poolCount = pool && pool.querySelector('[data-pool-count]');
  var slots = Array.prototype.slice.call(board.querySelectorAll('.team-slot'));
  function input(slot) { return slot.closest('.team-row').querySelector('input[type=hidden]'); }
  function chipOf(slot) { return slot.querySelector('.team-chip'); }
  var initial = slots.map(function (slot) { return { slot: slot, chip: chipOf(slot), value: input(slot).value }; });

  function refresh() {
    slots.forEach(function (slot) {
      var chip = chipOf(slot);
      input(slot).value = chip ? chip.dataset.token : '';
      slot.classList.toggle('empty', !chip);
      var row = slot.closest('.team-row');
      row.dataset.search = (row.querySelector('strong').textContent + ' ' + (chip ? chip.dataset.name : '')).toLowerCase();
    });
    if (poolCount) poolCount.textContent = poolChips.querySelectorAll('.team-chip').length;
    var dirty = initial.some(function (item) { return input(item.slot).value !== item.value; });
    if (dirtyNote) dirtyNote.hidden = !dirty;
    if (undo) undo.hidden = !dirty;
    if (save) save.disabled = !dirty;
  }
  function toPool(chip) {
    if (!poolChips) return false;
    // Em ordem alfabética na lista.
    var name = chip.dataset.name, after = null;
    poolChips.querySelectorAll('.team-chip').forEach(function (other) {
      if (!after && other !== chip && other.dataset.name.localeCompare(name, 'pt-BR') > 0) after = other;
    });
    poolChips.insertBefore(chip, after);
    return true;
  }
  // Põe o time na vaga: o que estava nela vai para a vaga de onde o time veio (troca) ou para a lista.
  function place(chip, slot) {
    var from = chip.closest('.team-slot'), there = chipOf(slot);
    if (from === slot) return;
    if (there) {
      if (from) from.appendChild(there);
      else if (!toPool(there)) return;  // modo livre: não há lista para onde voltar
    }
    slot.appendChild(chip);
    refresh();
  }
  function release(chip) {
    if (chip.closest('.team-slot') && toPool(chip)) refresh();
  }

  // --- Arrastar (mouse e toque, Pointer Events) ---
  var drag = null, raf = null;
  function targetAt(x, y) {
    var el = document.elementFromPoint(x, y);
    if (!el || !board.contains(el)) return null;
    var row = el.closest('.team-row');
    if (row) return row.querySelector('.team-slot');
    return el.closest('[data-pool]');
  }
  function highlight(target) {
    board.querySelectorAll('.drop-over').forEach(function (el) { if (el !== target) el.classList.remove('drop-over'); });
    if (target) target.classList.add('drop-over');
  }
  function autoScroll() {
    if (!drag || !drag.started) return;
    var edge = 60, y = drag.y;
    if (y < edge) window.scrollBy(0, -Math.ceil((edge - y) / 4));
    else if (y > window.innerHeight - edge) window.scrollBy(0, Math.ceil((y - window.innerHeight + edge) / 4));
    drag.target = targetAt(drag.x, drag.y);
    highlight(drag.target);
    raf = requestAnimationFrame(autoScroll);
  }
  board.addEventListener('pointerdown', function (e) {
    var chip = e.target.closest('.team-chip');
    if (!chip || e.button !== 0) return;
    if (e.pointerType !== 'mouse' && !e.target.closest('.group-grip')) return;
    drag = { chip: chip, id: e.pointerId, x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY, started: false };
    try { chip.setPointerCapture(e.pointerId); } catch (err) { /* ponteiro já solto */ }
  });
  board.addEventListener('pointermove', function (e) {
    if (!drag || e.pointerId !== drag.id) return;
    drag.x = e.clientX; drag.y = e.clientY;
    if (!drag.started) {
      if (Math.abs(drag.x - drag.x0) + Math.abs(drag.y - drag.y0) < 6) return;
      var rect = drag.chip.getBoundingClientRect();
      drag.started = true;
      drag.dx = drag.x0 - rect.left; drag.dy = drag.y0 - rect.top;
      drag.ghost = drag.chip.cloneNode(true);
      drag.ghost.classList.add('group-ghost');
      drag.ghost.style.width = rect.width + 'px';
      document.body.appendChild(drag.ghost);
      drag.chip.classList.add('dragging');
      board.classList.add('is-dragging');
      raf = requestAnimationFrame(autoScroll);
    }
    drag.ghost.style.transform = 'translate(' + (drag.x - drag.dx) + 'px,' + (drag.y - drag.dy) + 'px)';
    e.preventDefault();
  });
  function end(e, drop) {
    if (!drag || e.pointerId !== drag.id) return;
    if (drag.started) {
      var target = drop ? targetAt(e.clientX, e.clientY) : null;
      if (target && target.classList.contains('team-slot')) place(drag.chip, target);
      else if (target && target.hasAttribute('data-pool')) release(drag.chip);
      drag.ghost.remove();
      drag.chip.classList.remove('dragging');
      board.classList.remove('is-dragging');
      highlight(null);
      cancelAnimationFrame(raf);
    }
    drag = null;
  }
  board.addEventListener('pointerup', function (e) { end(e, true); });
  board.addEventListener('pointercancel', function (e) { end(e, false); });
  board.addEventListener('dragstart', function (e) { e.preventDefault(); });

  // --- Teclado ---
  board.addEventListener('keydown', function (e) {
    var chip = e.target.closest('.team-chip');
    if (!chip) return;
    var slot = chip.closest('.team-slot');
    if (slot && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      var index = slots.indexOf(slot) + (e.key === 'ArrowDown' ? 1 : -1);
      while (index >= 0 && index < slots.length && slots[index].closest('.team-row').hidden) {
        index += e.key === 'ArrowDown' ? 1 : -1;
      }
      e.preventDefault();
      if (index >= 0 && index < slots.length) { place(chip, slots[index]); chip.focus(); }
    } else if (slot && (e.key === 'Delete' || e.key === 'Backspace') && pool) {
      e.preventDefault();
      release(chip);
      chip.focus();
    } else if (!slot && e.key === 'Enter') {
      e.preventDefault();
      var free = slots.filter(function (s) { return !chipOf(s) && !s.closest('.team-row').hidden; })[0];
      if (free) { place(chip, free); chip.focus(); }
    }
  });

  // --- Buscas ---
  var rowFilter = board.querySelector('[data-row-filter]'), poolFilter = board.querySelector('[data-pool-filter]');
  function plain(s) { return (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim(); }
  if (rowFilter) rowFilter.addEventListener('input', function () {
    var q = plain(rowFilter.value);
    board.querySelectorAll('.team-row').forEach(function (row) { row.hidden = q && plain(row.dataset.search).indexOf(q) < 0; });
  });
  if (poolFilter) poolFilter.addEventListener('input', function () {
    var q = plain(poolFilter.value);
    poolChips.querySelectorAll('.team-chip').forEach(function (chip) { chip.hidden = q && plain(chip.dataset.name).indexOf(q) < 0; });
  });
  [rowFilter, poolFilter].forEach(function (field) {
    if (field) field.addEventListener('keydown', function (e) { if (e.key === 'Enter') e.preventDefault(); });
  });

  // --- Desfazer e saída sem salvar ---
  if (undo) undo.addEventListener('click', function () {
    initial.forEach(function (item) { if (item.chip) item.slot.appendChild(item.chip); });
    board.querySelectorAll('.team-slot .team-chip').forEach(function (chip) {
      if (!initial.some(function (item) { return item.chip === chip && item.slot === chip.parentNode; })) toPool(chip);
    });
    refresh();
  });
  var submitting = false;
  form.addEventListener('submit', function () { submitting = true; });
  window.addEventListener('beforeunload', function (e) {
    if (!submitting && save && !save.disabled) { e.preventDefault(); e.returnValue = ''; }
  });
  refresh();
})();
