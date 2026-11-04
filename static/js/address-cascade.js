/*
 * Cascading select wiring for the Address drawer form.
 * Wires up: country → state, state → city, city → union/ward.
 * Initialized per-call from _drawer.html when the spec is "address".
 */
(function () {
    function initCascade(root) {
        if (root._orbisAddressCascade) return;
        var country   = root.querySelector('#id_country');
        var state     = root.querySelector('#id_state');
        var city      = root.querySelector('#id_city');
        var unionWard = root.querySelector('#id_union_ward');
        if (!country || !state || !city) return;
        root._orbisAddressCascade = true;

        function reset(select) {
            if (!select) return;
            while (select.options.length > 1) select.remove(1);
        }

        function fillOptions(select, items, selectedId) {
            reset(select);
            items.forEach(function (item) {
                var o = document.createElement('option');
                o.value = item.id;
                o.textContent = item.label;
                if (selectedId && String(item.id) === String(selectedId)) o.selected = true;
                select.appendChild(o);
            });
        }

        async function loadStates(selectedId) {
            if (!country.value) { reset(state); reset(city); reset(unionWard); return; }
            var r = await fetch('/employees/ajax/states/?country=' + encodeURIComponent(country.value),
                                 { credentials: 'same-origin' });
            var j = await r.json();
            fillOptions(state, j.results, selectedId);
            reset(city);
            reset(unionWard);
        }

        async function loadCities(selectedId) {
            if (!state.value) { reset(city); reset(unionWard); return; }
            var r = await fetch('/employees/ajax/cities/?state=' + encodeURIComponent(state.value),
                                 { credentials: 'same-origin' });
            var j = await r.json();
            fillOptions(city, j.results, selectedId);
            reset(unionWard);
        }

        async function loadUnionWards(selectedId) {
            if (!unionWard) return;
            if (!city.value) { reset(unionWard); return; }
            var r = await fetch('/employees/ajax/union-wards/?city=' + encodeURIComponent(city.value),
                                 { credentials: 'same-origin' });
            var j = await r.json();
            fillOptions(unionWard, j.results, selectedId);
        }

        country.addEventListener('change', function () { loadStates(); });
        state.addEventListener('change',   function () { loadCities(); });
        city.addEventListener('change',    function () { loadUnionWards(); });
    }

    // Expose for the drawer partial to call after it's rendered.
    window.OrbisAddressCascade = { init: initCascade };

    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('form[data-section="address"]').forEach(initCascade);
    });

    // Re-init after any htmx content settle (covers drawer mounts).
    document.body && document.body.addEventListener('htmx:afterSettle', function (evt) {
        var form = evt.target.querySelector ? evt.target.querySelector('form[data-section="address"]') : null;
        if (form) initCascade(form);
    });
})();
