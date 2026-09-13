"""Notificações ao fim das execuções (Slack e e-mail).

Módulos: ``models`` (configuração por projeto), ``message`` (conteúdo),
``slack``, ``email`` (canais) e ``listener`` (integração com o ciclo de vida).
Este pacote não importa seus submódulos para evitar ciclos com ``projects``.
"""
