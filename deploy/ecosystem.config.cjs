module.exports = {
  apps: [
    {
      name: "water-pos-saif",
      cwd: __dirname + "/..",
      script: "./deploy/start.sh",
      interpreter: "/bin/bash",
      autorestart: true,
      watch: false,
      time: true,
      max_restarts: 10,
      restart_delay: 3000,
      kill_timeout: 5000,
      env: {
        NODE_ENV: "production",
      },
    },
  ],
};
