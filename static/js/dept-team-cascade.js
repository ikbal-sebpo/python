/*
 * Cascading select wiring for the user create/edit form.
 * Wires up: department → team → subteam.
 *
 * Only fires on an explicit *change* of the parent select — the
 * server-rendered selection is left untouched on initial load, so editing a
 * user preserves their saved team / sub-team.
 */
(function () {
    function init(root) {
        var dept = root.querySelector('#id_department');
        var team = root.querySelector('#id_team');
        var subteam = root.querySelector('#id_subteam');
        if (!dept || !team) return;

        function resetToPlaceholder(select) {
            if (!select) return;
            // Keep the leading empty "---------" option, drop the rest.
            var keep = (select.options.length && select.options[0].value === '') ? 1 : 0;
            while (select.options.length > keep) select.remove(keep);
        }

        async function fill(select, url) {
            resetToPlaceholder(select);
            var r = await fetch(url, { credentials: 'same-origin' });
            var j = await r.json();
            (j.results || []).forEach(function (item) {
                var o = document.createElement('option');
                o.value = item.id;
                o.textContent = item.label;
                select.appendChild(o);
            });
        }

        async function loadTeams() {
            // The sub-teams under the old team belong to a department the
            // user is leaving, so they go too — refilled once a team is picked.
            resetToPlaceholder(subteam);
            if (!dept.value) {
                resetToPlaceholder(team);
                return;
            }
            await fill(team, '/employees/ajax/teams/?department=' + encodeURIComponent(dept.value));
        }

        async function loadSubteams() {
            if (!subteam) return;
            if (!team.value) {
                resetToPlaceholder(subteam);
                return;
            }
            await fill(subteam, '/employees/ajax/subteams/?team=' + encodeURIComponent(team.value));
        }

        dept.addEventListener('change', loadTeams);
        team.addEventListener('change', loadSubteams);
    }

    document.addEventListener('DOMContentLoaded', function () {
        var form = document.querySelector('form[data-dept-team-cascade]');
        if (form) init(form);
    });
})();
