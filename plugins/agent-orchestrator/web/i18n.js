'use strict';
/*
 * Central UI dictionary (Spanish is the default language).
 *
 * Translation policy:
 *  - Only text owned by this interface is translated: labels, help, errors, dates.
 *  - Runner records are shown verbatim: worker messages, questions, answers, logs,
 *    file paths, job/plan/project IDs, CLI and model names, and worker-defined phases.
 *  - Internal identifiers (states, verdicts) are never renamed; they are only mapped to a
 *    display label through st(group, value). An identifier with no entry is shown as its raw
 *    value with underscores replaced, so a state added later is visible, never hidden.
 *  - A missing t(key) returns the key itself; tests assert every literal key used by the
 *    interface exists below.
 */
(() => {
  const es = {
    'a11y.skip': 'Saltar al proyecto',
    'common.notReported': 'Sin reportar',
    'common.unavailable': 'No disponible',
    'common.noActivity': 'Sin actividad todavía',
    'common.noProgress': 'Aún sin avances',
    'common.worker': 'Trabajador',
    'common.configuredCli': 'CLI configurado',
    'common.configuredModel': 'Modelo configurado',
    'common.awaitingUpdate': 'Esperando avance',
    'common.workerUpdate': 'Avance del trabajador',

    'time.lessThanMinute': '<1 min',
    'time.min': '{n} min',
    'time.hourMin': '{h} h {m} min',
    'time.justNow': 'Ahora mismo',

    'err.request': 'La solicitud local falló.',
    'err.timeout': 'El servidor local tardó demasiado en responder. Actualiza antes de reintentar una respuesta.',
    'err.auth': 'Abre la URL que imprime dashboard-web para autenticarte.',
    'err.boundary': 'Solo se aceptan solicitudes desde este equipo (loopback).',
    'err.notFound': 'El registro local no está disponible o no pertenece a este proyecto.',
    'err.notPending': 'Esta pregunta ya no está pendiente. Actualiza para ver su respuesta.',
    'err.tooLarge': 'La respuesta supera el límite de 64 KiB.',
    'err.badAnswer': 'Escribe una respuesta con texto antes de enviarla.',
    'err.save': 'No se pudo guardar la respuesta local. Actualiza antes de reintentar.',

    'summary.label': 'Resumen de todos los proyectos',
    'summary.running': 'En ejecución',
    'summary.waiting': 'Esperan respuesta',
    'summary.review': 'Por revisar',
    'summary.failed': 'Con fallo',
    'summary.done': 'Aceptados',

    'attention.title': 'Atención',
    'attention.empty': 'Nada requiere tu atención ahora.',
    'attention.coordinator': 'Coordinador del proyecto',
    'attention.question': 'Pregunta',
    'attention.review': 'Revisión',
    'attention.open': '{kind} · {project}',

    'connection.connecting': 'Conectando localmente',
    'connection.ok': 'Conectado localmente',
    'connection.paused': 'Actualización en pausa',
    'connection.lost': 'Conexión no disponible',
    'refresh.every': 'Se actualiza cada 2 segundos',
    'refresh.paused': 'Actualización automática en pausa',
    'refresh.pause': 'Pausar actualización',
    'refresh.resume': 'Reanudar actualización',

    'rail.label': 'Navegación de proyectos',
    'rail.projects': 'PROYECTOS',
    'rail.loading': 'Cargando proyectos…',
    'rail.privacy': 'Privado · solo este equipo',
    'rail.questions.one': '{n} pregunta esperando',
    'rail.questions.other': '{n} preguntas esperando',
    'rail.active': '{active} · {agents}',
    'rail.history': '{agents} · historial',
    'count.agents.one': '{n} agente',
    'count.agents.other': '{n} agentes',
    'count.questions.one': '{n} pregunta',
    'count.questions.other': '{n} preguntas',
    'count.plans.one': '{n} plan',
    'count.plans.other': '{n} planes',
    'count.active.one': '{n} activo',
    'count.active.other': '{n} activos',
    'count.files.one': '{n} archivo',
    'count.files.other': '{n} archivos',
    'count.filesUnknown': 'Archivos no disponibles',

    'agents.label': 'Navegación de agentes',
    'agents.title': 'Agentes',
    'agents.subtitle': 'Todos los agentes del proyecto',
    'agents.listLabel': 'Agentes del proyecto seleccionado',
    'agents.none': 'No hay agentes disponibles en este proyecto.',
    'agents.select': 'Selecciona un proyecto para ver sus agentes.',
    'agents.overview': 'Resumen del proyecto',
    'agents.overviewIdle': 'Todos los agentes, planes y consultas del proyecto',
    'agents.ready': 'Listo',

    'project.default': 'Tus proyectos',
    'project.connecting': 'Conectando con tus agentes locales…',
    'project.empty': 'Los trabajos y consultas del proyecto aparecerán aquí cuando existan.',
    'project.summary': '{agents} · {plans} · Actualizado {ago}',
    'project.agentWork': 'Trabajo del agente',

    'health.label': 'Estado del proyecto',
    'health.agentStatus': 'Estado del agente',
    'health.phase': 'Fase actual',
    'health.progressReported': 'Avance reportado',
    'health.noProgress': 'Sin avance',
    'health.elapsed': 'Tiempo transcurrido',
    'health.stillRunning': 'Sigue en ejecución',
    'health.latestRun': 'Última ejecución',
    'health.files': 'Archivos cambiados',
    'health.fromBaseline': 'Desde la línea base registrada',
    'health.overall': 'Salud general',
    'health.questionWaiting': 'Pregunta pendiente',
    'health.inProgress': 'En curso',
    'health.historyAvailable': 'Historial disponible',
    'health.noActive': 'Sin trabajos activos',
    'health.overallDetail': '{active} · {questions}',
    'health.reviewProgress': 'Avance de revisión',
    'health.acceptedOf': '{done} de {total} aceptados',
    'health.acceptedAria': 'Agentes aceptados tras revisión',
    'health.latestActivity': 'Última actividad',
    'health.localState': 'Estado local duradero',
    'health.reviewQueue': 'Cola de revisión',
    'health.failedHistory.one': '{n} ejecución fallida o fuera de alcance en el historial',
    'health.failedHistory.other': '{n} ejecuciones fallidas o fuera de alcance en el historial',

    'view.label': 'Vista de los agentes',
    'view.canvas': 'Lienzo',
    'view.list': 'Lista',
    'filter.label': 'Filtro',
    'filter.all': 'Todos los agentes',
    'filter.attention': 'Requieren atención',
    'filter.active': 'Activos',
    'filter.review': 'Por revisar',
    'filter.failed': 'Con fallo',
    'filter.complete': 'Aceptados',

    'canvas.label': 'Lienzo del proyecto: proyecto, planes y trabajos conectados',
    'canvas.help': 'Arrastra para mover el lienzo. Ctrl + rueda, o los botones, para acercar. Con el lienzo enfocado: flechas para mover, + y − para el zoom, 0 para ajustar. La vista Lista muestra lo mismo como tabla.',
    'canvas.controls': 'Controles del lienzo',
    'canvas.zoomIn': 'Acercar',
    'canvas.zoomOut': 'Alejar',
    'canvas.fit': 'Ajustar vista',
    'canvas.project': 'Proyecto',
    'canvas.plan': 'Plan',
    'canvas.planDone': 'Completo',
    'canvas.planProgress': 'Tareas: {done} de {total}',
    'canvas.planNoItems': 'Sin tareas en el plan',
    'canvas.projectStats': '{agents} · {plans}',
    'canvas.jobAria': '{name}: {state}. {work}',
    'canvas.noMatch': 'Ningún agente coincide con este filtro.',
    'canvas.empty': 'Aún no hay proyectos. Lanza un trabajo o solicita una consulta para empezar.',

    'table.agent': 'Agente',
    'table.model': 'Modelo / CLI',
    'table.task': 'Tarea actual',
    'table.phase': 'Fase',
    'table.elapsed': 'Tiempo',
    'table.state': 'Estado',
    'table.selected': 'Trabajo del agente seleccionado',
    'table.heading': 'Agentes ({n})',
    'table.noProjects': 'Aún no hay proyectos. Lanza un trabajo o solicita una consulta para empezar.',
    'table.noMatch': 'Ningún agente coincide con este filtro.',
    'table.noJobs': 'Este proyecto no tiene trabajos. Selecciona «Resumen del proyecto» para ver sus consultas.',
    'table.filesUsage': '{files} · Consumo {usage}',
    'table.usageReported': 'reportado',
    'table.usageUnavailable': 'no disponible',
    'table.workerUpdate': 'Avance del trabajador',
    'table.review': 'Revisión: {state}',

    'inspector.label': 'Inspector del agente seleccionado',
    'inspector.select': 'Selecciona un agente',
    'inspector.hint': 'Aquí aparecen los mensajes y las preguntas',
    'inspector.loading': 'Cargando detalles locales…',
    'inspector.loadingAgent': 'Cargando agente…',
    'inspector.coordinator': 'Coordinador del proyecto',
    'inspector.noProject': 'Ningún proyecto seleccionado',
    'inspector.selectProject': 'Selecciona un proyecto para empezar.',
    'inspector.projectDecisions': 'Decisiones del proyecto',
    'inspector.tabs': 'Detalles del agente',
    'inspector.messagesButton': 'Conversación',
    'tab.messages': 'Conversación',
    'tab.files': 'Archivos',
    'tab.tests': 'Pruebas',
    'tab.usage': 'Consumo',

    'chat.empty': 'Aún no hay mensajes.',
    'chat.waitingCoordinator': 'El coordinador del proyecto espera tu respuesta.',
    'chat.noQuestions': 'No hay preguntas pendientes del coordinador.',
    'author.worker': 'Trabajador',
    'author.coordinator': 'Coordinador',
    'author.runner': 'Ejecutor',
    'author.agent': 'Agente',
    'author.tool': 'Herramienta',
    'chat.result': 'Resultado final',
    'chat.error': 'Error del agente',
    'chat.failure': 'El trabajo terminó con fallo',
    'chat.clipped': 'Mensaje recortado a {n} caracteres.',
    'chat.dropped.one': 'Se omitió {n} mensaje anterior.',
    'chat.dropped.other': 'Se omitieron {n} mensajes anteriores.',
    'chat.thinking.one': '{n} bloque de razonamiento del modelo no se muestra.',
    'chat.thinking.other': '{n} bloques de razonamiento del modelo no se muestran.',
    'chat.logTruncated': 'El registro es muy grande: solo se interpreta su parte final.',
    'chat.unrecognized.one': '{n} línea del registro tiene un formato no reconocido; consulta los extractos de registro.',
    'chat.unrecognized.other': '{n} líneas del registro tienen un formato no reconocido; consulta los extractos de registro.',
    'event.review': 'Revisión: {verdict}',
    'event.job_created': 'Trabajo creado',
    'event.agent_started': 'Agente iniciado',
    'event.agent_finished': 'Agente terminado',
    'event.worker_progress': 'Avance del trabajador',
    'event.question_asked': 'Pregunta del trabajador',
    'event.question_answered': 'Pregunta respondida',
    'event.question_expired': 'Pregunta caducada',
    'event.feedback_requested': 'Consulta enviada al coordinador',
    'event.review_recorded': 'Revisión registrada',
    'event.cancellation_requested': 'Cancelación solicitada',
    'event.scope_violation': 'Violación de alcance',
    'event.timeout_reached': 'Tiempo límite alcanzado',
    'event.terra_fallback_started': 'Trabajo de respaldo iniciado',

    'question.heading': 'Se necesita una aclaración',
    'question.saved': 'Respuesta guardada',
    'question.answer': 'Tu respuesta',
    'question.send': 'Enviar respuesta',
    'question.empty': 'Escribe una respuesta antes de enviarla.',
    'question.saving': 'Guardando tu respuesta…',
    'question.savedOk': 'Respuesta guardada. El agente puede continuar.',
    'question.savedNotice': 'Respuesta guardada localmente.',
    'question.gone': 'Esta pregunta ya no está pendiente. Tu borrador se conserva.',
    'question.sourceCoordinator': 'Coordinador del proyecto',
    'question.sourceWorker': 'Trabajador · {agent}',
    'question.label': 'Preguntas que esperan tu respuesta',
    'history.title': 'Preguntas respondidas y cerradas',
    'history.label': 'Preguntas respondidas',
    'history.meta': '{source} · {state} · {time}',
    'history.closed': 'Cerrada sin respuesta.',

    'files.noBaseline': 'No hay línea base para calcular cambios: el espacio de trabajo no es un repositorio Git o no está accesible.',
    'files.fromTools': 'Escritos por el agente con sus herramientas',
    'files.noToolWrites': 'El agente no registró escrituras de archivos con herramientas.',
    'files.count.one': '{n} archivo cambiado respecto a la línea base registrada.',
    'files.count.other': '{n} archivos cambiados respecto a la línea base registrada.',
    'files.select': 'Selecciona un agente trabajador para ver estos detalles.',
    'tests.review': 'Revisión: {state}',
    'tests.recorded': 'Registrado por {reviewer} · {time}',
    'tests.evidence': 'Evidencia de pruebas',
    'tests.notes': 'Notas de la revisión',
    'tests.none': 'No hay evidencia independiente de pruebas. Los resultados no se infieren de los registros.',
    'usage.reported': 'Última lectura reportada por el proveedor. Puede no ser el consumo acumulado.',
    'usage.none': 'Consumo no disponible. Este CLI no ha reportado tokens ni costo.',

    'summary.title': 'Resumen',
    'summary.elapsed': 'Tiempo {time}',
    'summary.files': 'Archivos cambiados',
    'summary.fromBaseline': 'Desde la línea base',
    'summary.fromTools': 'Escritos con herramientas',
    'summary.noBaseline': 'Sin Git: sin línea base',
    'summary.cache': '{cost} · caché {cache}',
    'summary.tests': 'Evidencia de pruebas',
    'summary.reviewRecords': 'Registros de revisión',
    'summary.notReported': 'Sin reportar',
    'summary.tokens': 'Uso de tokens',
    'summary.costUnavailable': 'Costo no disponible',
    'summary.agentLabel': 'Resumen del agente',

    'logs.title': 'Extractos de registro locales / privados',
    'logs.help': 'Solo los últimos 8 KiB de cada flujo del agente seleccionado. Puede contener contenido privado del repositorio.',
    'logs.stdout': 'Salida estándar',
    'logs.stderr': 'Error estándar',
    'logs.noOutput': 'Aún no hay salida.',
    'logs.noErrors': 'Sin errores reportados.',

    'notice.unreadable.one': 'Se omitió {n} registro local ilegible.',
    'notice.unreadable.other': 'Se omitieron {n} registros locales ilegibles.',

    'state.starting': 'Iniciando',
    'state.running': 'En ejecución',
    'state.needs_input': 'Espera respuesta',
    'state.cancelling': 'Cancelando',
    'state.cancelled': 'Cancelado',
    'state.failed': 'Fallido',
    'state.lost': 'Desconectado',
    'state.timed_out': 'Tiempo agotado',
    'state.scope_violated': 'Fuera de alcance',
    'state.succeeded': 'Terminado',
    'state.awaiting_review': 'Pendiente de revisión',
    'state.repair_required': 'Requiere corrección',
    'state.rejected': 'Rechazado',
    'state.accepted': 'Aceptado',
    'state.pending': 'Pendiente',
    'state.answered': 'Respondida',
    'state.expired': 'Caducada',
    'state.terra_fallback_started': 'Respaldo iniciado',

    'review.required': 'Requerida',
    'review.not_ready': 'Aún no',
    'review.accepted': 'Aceptada',
    'review.repair_required': 'Requiere corrección',
    'review.rejected': 'Rechazada',

    'verdict.accepted': 'Aceptado',
    'verdict.repair_required': 'Requiere corrección',
    'verdict.rejected': 'Rechazado',

    'usage.input_tokens': 'Tokens de entrada',
    'usage.output_tokens': 'Tokens de salida',
    'usage.total_tokens': 'Tokens totales',
    'usage.total_cost_usd': 'Costo total (USD)',
    'usage.cached_input_tokens': 'Tokens de entrada en caché',
    'usage.thinking_tokens': 'Tokens de razonamiento',
    'usage.cache_read_input_tokens': 'Tokens leídos de caché',
    'usage.cache_creation_input_tokens': 'Tokens escritos en caché',
    'usage.duration_ms': 'Duración',
    'usage.num_turns': 'Turnos',
    'usage.other': 'Otros campos del proveedor ({n})',
  };

  const dictionaries = { es };
  const language = 'es';
  const dictionary = dictionaries[language];
  const locale = language;

  const raw = value => String(value).replaceAll('_', ' ');
  const fill = (text, vars) => text.replace(/\{(\w+)\}/g, (match, name) =>
    vars && Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match);

  const t = (key, vars) => fill(Object.prototype.hasOwnProperty.call(dictionary, key) ? dictionary[key] : key, vars);
  // Plural helper: tn('rail.questions', 3) reads 'rail.questions.other'.
  const tn = (base, n, vars) => t(`${base}.${n === 1 ? 'one' : 'other'}`, { n, ...vars });
  // Identifier -> label. Unknown identifiers stay visible as their raw value.
  const st = (group, value) => {
    if (value === null || value === undefined || value === '') return t('common.notReported');
    const key = `${group}.${value}`;
    return Object.prototype.hasOwnProperty.call(dictionary, key) ? dictionary[key] : raw(value);
  };

  const number = value => Number(value).toLocaleString(locale);
  const clockFormat = new Intl.DateTimeFormat(locale, { hour: 'numeric', minute: '2-digit' });
  const dateFormat = new Intl.DateTimeFormat(locale, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  const relativeFormat = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
  const time = value => clockFormat.format(new Date(value));
  const dateLong = value => dateFormat.format(new Date(value));
  function relative(value) {
    const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
    if (seconds < 60) return t('time.justNow');
    if (seconds < 3600) return relativeFormat.format(-Math.floor(seconds / 60), 'minute');
    if (seconds < 86400) return relativeFormat.format(-Math.floor(seconds / 3600), 'hour');
    return dateFormat.format(new Date(value));
  }

  window.I18N = { t, tn, st, number, time, dateLong, relative, raw, language, dictionary };
})();
