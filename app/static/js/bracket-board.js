/* Ordem dinâmica do mata-mata, em Gerenciar (manage.html #chaveamento): o organizador
   arrasta os jogadores entre as posições das chaves iniciais. Cada posição leva um
   <input type=hidden name=posicao_N> com a inscrição (ou vazio, "Sem adversário"),
   que vai no "Salvar chaveamento" / "Confirmar chaveamento" (tournaments.arrange_bracket).

   - Soltar um jogador numa posição troca os dois de lugar (o "Sem adversário" também
     se arrasta: quem fica com ele passa direto ou espera na repescagem).
   - Confronto sem nenhum jogador não pode: a troca é desfeita, com o aviso.
   - Mouse: arrasta pelo cartão inteiro. Toque: pela alça (⠿), para o resto do
     cartão continuar rolando a página. Perto da borda da tela, a página rola.
   - Teclado: com o cartão em foco, ↑ e ↓ trocam com a posição de cima/de baixo. */
(function () {
  var board = document.getElementById('bracket-board');
  if (!board) return;
  var form = board.closest('form');
  var save = form.querySelector('[data-save]'), undo = form.querySelector('[data-undo]');
  var dirtyNote = form.querySelector('[data-dirty]'), errorBox = form.querySelector('[data-bracket-error]');
  var repechage = board.dataset.repechage === '1';
  var slots = Array.prototype.slice.call(board.querySelectorAll('.bracket-slot'));
  var pairs = Array.prototype.slice.call(board.querySelectorAll('.bracket-pair'));
  var initial = slots.map(function (slot) { return slot.querySelector('.bracket-chip'); });

  function chipOf(slot) { return slot.querySelector('.bracket-chip'); }
  function isBye(chip) { return chip.dataset.entry === ''; }
  function place(slot, chip) {
    slot.insertBefore(chip, slot.querySelector('input'));
    slot.querySelector('input').value = chip.dataset.entry;
    chip.setAttribute('aria-label', chip.dataset.name + ', confronto ' + (Math.floor(slots.indexOf(slot) / 2) + 1) +
      '. Arraste ou use as setas para cima e para baixo para trocar de posição.');
  }
  function pairState(index) {
    var a = chipOf(slots[index * 2]), b = chipOf(slots[index * 2 + 1]);
    return { a: a, b: b, empty: isBye(a) && isBye(b), bye: isBye(a) !== isBye(b) };
  }

  function refresh() {
    pairs.forEach(function (pair, index) {
      var state = pairState(index), note = pair.querySelector('[data-pair-note]');
      var neighbor = pairs[index ^ 1] ? pairState(index ^ 1) : null;
      if (!note) return;
      if (state.bye) {
        var who = isBye(state.a) ? state.b.dataset.name : state.a.dataset.name;
        note.textContent = repechage && neighbor && !neighbor.bye && !neighbor.empty
          ? who + ' espera na repescagem o perdedor do confronto ' + ((index ^ 1) + 1) + '.'
          : who + ' passa direto para a fase seguinte.';
      } else note.textContent = '';
    });
    var dirty = slots.some(function (slot, i) { return chipOf(slot) !== initial[i]; });
    if (dirtyNote) dirtyNote.hidden = !dirty;
    if (undo) undo.hidden = !dirty;
    if (save) save.disabled = !dirty;
  }

  function showError(text) {
    if (!errorBox) return;
    errorBox.textContent = text || '';
    errorBox.hidden = !text;
  }

  // Troca o cartão com o da posição `target`; desfaz se algum confronto ficar sem jogador.
  function swap(chip, target) {
    var from = chip.parentElement;
    if (!target || target === from) return false;
    var other = chipOf(target);
    place(target, chip);
    place(from, other);
    var a = Math.floor(slots.indexOf(from) / 2), b = Math.floor(slots.indexOf(target) / 2);
    if (pairState(a).empty || pairState(b).empty) {
      place(from, chip);
      place(target, other);
      showError('Cada confronto precisa ter pelo menos um jogador: essa troca deixaria um confronto sem ninguém.');
      return false;
    }
    showError('');
    refresh();
    return true;
  }

  // --- Arrastar (mouse e toque, Pointer Events) ---
  var drag = null, raf = null;
  function slotAt(x, y) {
    var el = document.elementFromPoint(x, y);
    var slot = el && el.closest('.bracket-slot');
    return slot && board.contains(slot) ? slot : null;
  }
  function highlight(slot) {
    slots.forEach(function (s) { s.classList.toggle('drop-over', s === slot && s !== (drag && drag.chip.parentElement)); });
  }
  function autoScroll() {
    if (!drag || !drag.started) return;
    var edge = 60, y = drag.y;
    if (y < edge) window.scrollBy(0, -Math.ceil((edge - y) / 4));
    else if (y > window.innerHeight - edge) window.scrollBy(0, Math.ceil((y - window.innerHeight + edge) / 4));
    highlight(slotAt(drag.x, drag.y));
    raf = requestAnimationFrame(autoScroll);
  }
  board.addEventListener('pointerdown', function (e) {
    var chip = e.target.closest('.bracket-chip');
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
      if (drop) swap(drag.chip, slotAt(e.clientX, e.clientY));
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

  // --- Teclado: ↑ e ↓ trocam com a posição de cima/de baixo ---
  board.addEventListener('keydown', function (e) {
    var chip = e.target.closest('.bracket-chip');
    if (!chip || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
    e.preventDefault();
    var next = slots.indexOf(chip.parentElement) + (e.key === 'ArrowDown' ? 1 : -1);
    if (next >= 0 && next < slots.length) {
      swap(chip, slots[next]);
      chip.focus();
    }
  });

  // --- Desfazer: volta cada um à posição salva ---
  if (undo) undo.addEventListener('click', function () {
    slots.forEach(function (slot, i) { place(slot, initial[i]); });
    showError('');
    refresh();
  });

  // --- Confirmar: a janela mostra os confrontos como vão ficar ---
  var summary = document.querySelector('[data-bracket-summary]');
  var opener = form.querySelector('[data-confirm-open]');
  if (opener && summary) opener.addEventListener('click', function () {
    summary.textContent = '';
    pairs.forEach(function (pair, index) {
      var state = pairState(index), li = document.createElement('li');
      li.textContent = state.bye ? (isBye(state.a) ? state.b : state.a).dataset.name + ' (sem adversário)'
        : state.a.dataset.name + ' x ' + state.b.dataset.name;
      summary.appendChild(li);
    });
  });

  // Sair da página com mudanças não salvas: o navegador pergunta.
  var submitting = false;
  form.addEventListener('submit', function () { submitting = true; });
  window.addEventListener('beforeunload', function (e) {
    if (!submitting && save && !save.disabled) { e.preventDefault(); e.returnValue = ''; }
  });
  slots.forEach(function (slot) { place(slot, chipOf(slot)); });
  refresh();
})();
