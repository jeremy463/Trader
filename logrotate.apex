# The app already rotates its own files (5 x 5 MB). This covers anything else written under /var/log/apex.
/var/log/apex/*.log { weekly rotate 8 compress delaycompress missingok notifempty copytruncate }
