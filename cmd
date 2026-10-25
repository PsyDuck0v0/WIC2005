h1 traceroute -n -I -m 5 -w 1 -q 1 h4 > evidence3/failure.txt
r1 ip route get h4 >> evidence3/failure.txt 2>&1