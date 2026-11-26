/* Etapas do cadastro de campeonato (Básico, Patch, Formato, Participantes, Premiação, Administradores).

   <div data-steps="wizard">: criar campeonato (new.html). Uma etapa por vez; "Avançar" confere os
   campos da etapa antes de seguir e "Voltar" volta; o menu das etapas só leva às já visitadas. O
   botão de criar fica na última etapa.
   <div data-steps="tabs">: Gerenciar (manage.html). As etapas viram abas livres. O endereço #<id>
   de um elemento dentro de uma aba (#iso, #inscritos, #etapa-formato...) abre essa aba, e o campo
   "etapa" dos formulários guarda a aba aberta, para o Gerenciar voltar a ela depois de salvar.

   Nos formulários com campos nas etapas (<form novalidate data-step-validate>), ao enviar: o
   primeiro campo inválido abre a etapa dele e mostra o aviso do navegador, em vez do envio falhar
   em silêncio por causa de um campo escondido. */
(function () {
  document.querySelectorAll('[data-steps]').forEach(function (root) {
    var wizard = root.dataset.steps === 'wizard';
    var panels = Array.prototype.slice.call(root.querySelectorAll('.t-step'));
    var tabs = Array.prototype.slice.call(root.querySelectorAll('[data-step-go]'));
    var prev = root.querySelector('[data-step-prev]'), next = root.querySelector('[data-step-next]');
    var finish = root.querySelector('[data-step-finish]');
    var current = 0, reached = 0;

    function show(index, scroll) {
      current = Math.max(0, Math.min(panels.length - 1, index));
      reached = Math.max(reached, current);
      panels.forEach(function (panel, i) { panel.hidden = i !== current; });
      tabs.forEach(function (tab, i) {
        var on = i === current;
        tab.classList.toggle('active', on);
        tab.classList.toggle('done', wizard && i < reached && !on);
        tab.setAttribute('aria-selected', on ? 'true' : 'false');
        if (wizard) tab.disabled = i > reached;
      });
      if (prev) prev.hidden = current === 0;
      if (next) next.hidden = current === panels.length - 1;
      if (finish) finish.hidden = current !== panels.length - 1;
      var step = panels[current].dataset.step;
      document.querySelectorAll('input[name="etapa"]').forEach(function (input) { input.value = step; });
      if (scroll) root.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
    // O primeiro campo inválido da etapa (os desativados e escondidos por formato não contam).
    function invalidIn(panel) {
      var fields = panel.querySelectorAll('input, select, textarea');
      for (var i = 0; i < fields.length; i++) {
        if (fields[i].willValidate && !fields[i].checkValidity()) return fields[i];
      }
      return null;
    }
    tabs.forEach(function (tab, i) {
      tab.addEventListener('click', function () { if (!wizard || i <= reached) show(i); });
    });
    if (prev) prev.addEventListener('click', function () { show(current - 1, true); });
    if (next) next.addEventListener('click', function () {
      var bad = invalidIn(panels[current]);
      if (bad) { bad.reportValidity(); return; }
      show(current + 1, true);
    });
    // Enter num campo do cadastro avança a etapa, em vez de enviar o formulário antes da hora.
    if (wizard) root.addEventListener('keydown', function (e) {
      if (e.defaultPrevented || e.key !== 'Enter' || e.target.tagName !== 'INPUT' || e.target.type === 'submit') return;
      if (current < panels.length - 1) { e.preventDefault(); next.click(); }
    });
    root.stepsShow = function (panel) { show(panels.indexOf(panel), false); };

    // Etapa inicial: a do elemento do endereço (#id), senão a primeira.
    var target = location.hash ? document.getElementById(decodeURIComponent(location.hash.slice(1))) : null;
    var start = 0;
    if (target) {
      var holder = target.closest('.t-step');
      if (holder && panels.indexOf(holder) >= 0) start = panels.indexOf(holder);
    }
    // Formulário devolvido pelo servidor (com erro): todas as etapas já foram preenchidas.
    reached = wizard && root.dataset.reached !== 'all' ? 0 : panels.length - 1;
    show(start, false);
    if (target && start) setTimeout(function () { target.scrollIntoView({ block: 'start' }); }, 0);
    // Link para um #id da mesma página (ex.: "Grupos" no aviso do sorteio): abre a aba dele.
    if (!wizard) window.addEventListener('hashchange', function () {
      var el = document.getElementById(decodeURIComponent(location.hash.slice(1)));
      var holder = el && el.closest('.t-step');
      if (holder && panels.indexOf(holder) >= 0) {
        show(panels.indexOf(holder), false);
        el.scrollIntoView({ block: 'start' });
      }
    });
  });

  // Ao enviar: o primeiro campo inválido abre a etapa dele (antes dos outros scripts de envio).
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.hasAttribute('data-step-validate') || (e.submitter && e.submitter.formNoValidate)) return;
    var fields = form.elements;
    for (var i = 0; i < fields.length; i++) {
      var field = fields[i];
      if (!field.willValidate || field.checkValidity()) continue;
      var panel = field.closest('.t-step'), root = panel && panel.closest('[data-steps]');
      if (root && root.stepsShow) root.stepsShow(panel);
      e.preventDefault();
      e.stopImmediatePropagation();
      field.reportValidity();
      return;
    }
  }, true);
})();
