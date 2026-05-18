// pm2 ecosystem config — Aste Giudiziarie
// Avvio:    pm2 start ecosystem.config.js
// Restart:  pm2 restart aste-giudiziarie
// Logs:     pm2 logs aste-giudiziarie
module.exports = {
  apps: [{
    name: "aste-giudiziarie",
    cwd: "/root/aste-giudiziarie",
    script: "./.venv/bin/python",
    args: "server.py --host 127.0.0.1 --port 3001",
    interpreter: "none",            // python è già lo script eseguibile
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: "500M",
    merge_logs: true,
    out_file: "/var/log/aste-giudiziarie.out.log",
    error_file: "/var/log/aste-giudiziarie.err.log",
    time: true,
    // Le env var sono caricate da server.py via python-dotenv da /etc/aste-giudiziarie/.env
    env: {
      PYTHONUNBUFFERED: "1",
      TZ: "Europe/Rome"
    }
  }]
};
