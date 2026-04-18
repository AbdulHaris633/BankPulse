<?php
	namespace ProxyChecker;
	include __DIR__ . '/ProxyChecker.php';
	
	if (isset($_POST['proxies'])) {
		$proxyList = explode("\n", $_POST['proxies']);
		$pingUrl = 'https://icanhazip.com/';
		$proxies = array();
		foreach($proxyList as $nextProxy) {
			array_push($proxies, trim($nextProxy));
		}
		$proxyChecker = new ProxyChecker($pingUrl);
		$results = $proxyChecker->checkProxies($proxies);
	}
?>

<html>
	<head>
		<title>ProxyChecker</title>
		<style>
			.container {
				width: 100%;
				height: 100%;
				display: flex;
				justify-content: space-around;
				margin-top: 60px;
			}
			div.proxy-form {
				width: 25%;
				height: 400px;
			}
			div.instructions {
				margin-top: 60px;
			}
			textarea {
				resize: none;
				height: 100%;
				width: 100%;
			}
			span.special {
				display: block;
				font-size: 0.9em;
				margin-bottom: 5px;
			}
			div.results-list {
				width: 25%;
				height: 400px;		
				overflow-y: scroll;
				border: 1px solid black;
				border-radius: 4px;
			}
			table {
				margin: 0px;
			}
			td {
				text-align: center;
				width: 100px;
			}
		</style>
	</head>
	<body>
		<h1>Proxy Checker</h1>
		<div class="container">
			<div class="instructions">
				<pre>
# Usage
## Proxy format
    {ip}:{port},{user:password},{type}

type - http, socks4, socks5 
user:password and type not required 

Some examples:

    123.456.789:8080
    123.456.789:8080,user:pass
    123.456.789:8080,user:pass,socks5

				</pre>
			</div>
			<div class="proxy-form">
				<h3>Proxy List:</h3>
				<form method="POST">
					<textarea name="proxies"></textarea>
					<br>
					<br>
					<button style="float: right;" type="submit">Check Proxies</button>
				</form>
			</div>
			<div class="results-list">
				<h3>Proxy Status:</h3>
				<table>
					<tr>
						<th>IP</th><th>Port</th><th>Status</th><th>Speed</th>
					</tr>
					<?php
						if(isset($results)){
							foreach($results as $result) {
								$info = $result['info'];
								$http_code = $info['http_code'];
								$connect_time = $info['total_time'];
								$primary_ip = $info['primary_ip'];
								$primary_port = $info['primary_port'];
								if($http_code == 200) {
									$html = '<tr><td>'.$primary_ip.'</td><td>'.$primary_port.'</td>';
									$html .= '<td style="color: #53f153;">Online</td>';
									$html .= '<td>'.$connect_time.'</td></tr>';
									echo $html;
								}
							}
						}
					?>
				</table>
			</div>
		</div>
	</body>
</html>




























