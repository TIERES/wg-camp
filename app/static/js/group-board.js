/* Grupos dinâmicos, em Gerenciar (manage.html #grupos): o organizador arrasta os
   jogadores entre os grupos. Cada jogador leva um <input type=hidden name=grupo_ID>
   com o grupo dele, que vai no "Salvar grupos" (tournaments.arrange_groups).

   - Mouse: arrasta pelo cartão inteiro. Toque: pela alça (⠿), para o resto do
     cartão continuar rolando a página. Perto da borda da tela, a página rola.
   - Teclado: com o cartão em foco, ← e → levam o jogador ao grupo anterior/seguinte.
   - "Sem grupo" só entrega jogadores: não recebe.
   - Grupo acima do limite de vagas fica marcado (o organizador pode, sem o limite). */
(function () {
  var board = document.getElementById('group-board');
  if (!board) return;
  var form = board.closest('form');
  var save = form.querySelector('[data-save]'), undo = form.querySelector('[data-undo]');
  var dirtyNote = form.querySelector('[data-dirty]');
  var zones = Array.prototype.slice.call(board.querySelectorAll('.group-drop[data-slot]'));
  var targets = zones.filter(function (z) { return z.dataset.slot !== ''; });
  var initial = {};
  board.querySelectorAll('.group-chip').forEach(function (chip) {
    initial[chip.dataset.entry] = { slot: chip.querySelector('input').value, zone: chip.parentElement };
  });

  function refresh() {
    zones.forEach(function (zone) {
      var column = zone.closest('.group-column'), count = zone.querySelectorAll('.group-chip').length;
      var cap = parseInt(column.dataset.capacity, 10), label = column.querySelector('[data-count]');
      if (label && cap) {
        label.textContent = count + ' de ' + cap + (count > cap ? ' (acima do limite)' : '');
        column.classList.toggle('over', count > cap);
      }
      zone.classList.toggle('empty', count === 0);
      if (zone.dataset.slot === '' && count === 0) column.hidden = true;
    });
    var dirty = Object.keys(initial).some(function (id) {
      return board.querySelector('.group-chip[data-entry="' + id + '"] input').value !== initial[id].slot;
    });
    if (dirtyNote) dirtyNote.hidden = !dirty;
    if (undo) undo.hidden = !dirty;
    if (save) save.disabled = !dirty;
  }

  function moveTo(chip, zone) {
    if (!zone || zone.dataset.slot === '' || chip.parentElement === zone) return;
    zone.appendChild(chip);
    chip.querySelector('input').value = zone.dataset.slot;
    chip.setAttribute('aria-label', chip.dataset.name + ', ' + zone.dataset.name +
      '. Arraste ou use as setas para mudar de grupo.');
    refresh();
  }

  // --- Arrastar (mouse e toque, Pointer Events) ---
  var drag = null, raf = null;
  function zoneAt(x, y) {
    var el = document.elementFromPoint(x, y);
    var zone = el && el.closest('.group-column') && el.closest('.group-column').querySelector('.group-drop');
    return zone && board.contains(zone) && zone.dataset.slot !== '' ? zone : null;
  }
  function highlight(zone) {
    targets.forEach(function (z) { z.closest('.group-column').classList.toggle('drop-over', z === zone); });
  }
  function autoScroll() {
    if (!drag || !drag.started) return;
    var edge = 60, y = drag.y;
    if (y < edge) window.scrollBy(0, -Math.ceil((edge - y) / 4));
    else if (y > window.innerHeight - edge) window.scrollBy(0, Math.ceil((y - window.innerHeight + edge) / 4));
    drag.zone = zoneAt(drag.x, drag.y);
    highlight(drag.zone);
    raf = requestAnimationFrame(autoScroll);
  }
  board.addEventListener('pointerdown', function (e) {
    var chip = e.target.closest('.group-chip');
    if (!chip || e.button !== 0) return;
    // No toque, só a alça arrasta (o resto do cartão rola a página).
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
      drag.ghost.removeAttribute('id');
      drag.ghost.querySelector('input').removeAttribute('name');
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
      if (drop) moveTo(drag.chip, zoneAt(e.clientX, e.clientY));
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
  // Sem o arraste nativo do navegador (imagens e links do cartão).
  board.addEventListener('dragstart', function (e) { e.preventDefault(); });

  // --- Teclado: ← e → mudam o jogador de grupo ---
  board.addEventListener('keydown', function (e) {
    var chip = e.target.closest('.group-chip');
    if (!chip || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return;
    e.preventDefault();
    var index = targets.indexOf(chip.parentElement);
    var next = index < 0 ? (e.key === 'ArrowRight' ? 0 : targets.length - 1)
      : index + (e.key === 'ArrowRight' ? 1 : -1);
    if (next >= 0 && next < targets.length) {
      moveTo(chip, targets[next]);
      chip.focus();
    }
  });

  // --- Desfazer: volta cada um ao grupo salvo ---
  if (undo) undo.addEventListener('click', function () {
    Object.keys(initial).forEach(function (id) {
      var chip = board.querySelector('.group-chip[data-entry="' + id + '"]');
      initial[id].zone.closest('.group-column').hidden = false;
      initial[id].zone.appendChild(chip);
      chip.querySelector('input').value = initial[id].slot;
    });
    refresh();
  });
  // Sair da página com mudanças não salvas: o navegador pergunta.
  var submitting = false;
  form.addEventListener('submit', function () { submitting = true; });
  window.addEventListener('beforeunload', function (e) {
    if (!submitting && save && !save.disabled) { e.preventDefault(); e.returnValue = ''; }
  });
  refresh();
})();
