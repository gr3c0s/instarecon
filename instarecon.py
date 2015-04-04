#!/usr/bin/env python
import sys
import socket
import argparse
import requests
#from abc import ABCMeta, abstractmethod
import re
import time
from random import randint

import pythonwhois as whois #http://cryto.net/pythonwhois/usage.html https://github.com/joepie91/python-whois
from ipwhois import IPWhois as ipw #https://pypi.python.org/pypi/ipwhois
import ipaddress as ipa #https://docs.python.org/3/library/ipaddress.html
import dns.resolver,dns.reversename,dns.name #http://www.dnspython.org/docs/1.12.0/
import shodan #https://shodan.readthedocs.org/en/latest/index.html


class Host(object):
    '''
    Host being scanned. 
    Contains several IP and Domain objects.
    '''

    def __init__(self,domain=None,ips=[]):
        
        if ips and domain: #target is domain, but ip was already resolved by scan.add_host()
            self.type = 'domain'
        elif ips: #target is ip
            self.type = 'ip'
        elif domain: #target is domain
            self.type = 'domain'
        else:
            raise ValueError


        #Single domain
        self.domain = domain
        
        #Multiple IPs
        self.ips = [ IP(str(ip)) for ip in ips ]

        #MX records - will contain instances of Host
        self.mx = set()
        #NS records - will be an instance of Host
        self.ns = set()

        #Whois results for self.domain (str)
        self.whois_domain = None

        #Company LinkedIn page (str)
        self.linkedin_page = None

        #Subdomains, found through google dorks and reverse cidr lookups
        #list of Hosts instances
        self.subdomains = set()

        #Will hold all cidrs and whois_ip related for self.ips
        #cidrs as key and whois_ip as values
        self.cidrs = {}


    def __str__(self):
        if self.type == 'domain':
            return str(self.domain)
        elif self.type == 'ip':
            return str(self.ips[0])
    
    def __hash__(self):
        if self.type == 'domain':
            return hash(('domain',self.domain))
        elif self.type == 'ip':
            return hash(('ip',self.ips))
    
    def __eq__(self,other):
        if self.type == 'domain':
            return self.domain == other.domain
        elif self.type == 'ip':
            return self.ips == other.ips

    def get_ips(self):
        if self.domain and not self.ips:
            ips = self._ret_host_by_name(self.domain)
            if ips:
                self.ips = [ IP(str(ip)) for ip in ips ]
        return self

    def dns_lookups(self):
        if self.type == 'domain':
            self.get_ips()
            for ip in self.ips: ip.get_rev_domains() 

        if self.type == 'ip':
            for ip in self.ips: ip.get_rev_domains() 

        return self

    def mx_dns_lookup(self):
        try:
            if self.domain:
                mx_list = self._ret_mx_by_name(self.domain)
                self.mx = set([ Host(mx,self._ret_host_by_name(mx)).get_rev_domains_for_ips() for mx in mx_list ])

        except Exception as e:
            Scan.error('# [-] MX lookup failed for '+self.domain,sys._getframe().f_code.co_name)

    def ns_dns_lookup(self):
        try:
            if self.domain:
                ns_list = self._ret_ns_by_name(self.domain)
                self.ns = set([ Host(ns,self._ret_host_by_name(ns)).get_rev_domains_for_ips() for ns in ns_list ])

        except Exception as e:
            Scan.error('# [-] NS lookup failed for '+self.domain,sys._getframe().f_code.co_name)

    def get_rev_domains_for_ips(self):
        if self.ips:
            for ip in self.ips:
                ip.get_rev_domains()
        return self

    def get_whois_domain(self,num=0):
        try:
            if self.domain:
                query = whois.get_whois(self.domain)
                
                if 'raw' in query:
                    self.whois_domain = query['raw'][0].lstrip().rstrip()

        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)
            pass
    
    def get_all_whois_ip(self):
        #Gets whois_ip for each IP in self.ips
        for ip in self.ips:

            cidr_found = False

            #Iterate cidrs related to Host to see if it already exists 
            for cidr,whois_ip in self.cidrs.iteritems():
                if ipa.ip_address(ip.ip.decode('unicode-escape')) in ipa.ip_network(cidr.decode('unicode-escape')):
                    #If cidr is already in Host, we won't get_whois_ip again.
                    #Instead we will save cidr in ip just to make it easier to relate them later
                    ip.cidr,ip.whois = cidr,whois_ip
                    cidr_found = True
                    break

            if not cidr_found:
                ip.get_whois_ip()
                self.cidrs[ip.cidr] = ip.whois_ip

    def get_all_shodan(self,key):
        if key:
            for ip in self.ips:
                ip.get_shodan(key)

    @staticmethod
    def _ret_host_by_name(name):
        try:
            return dns.resolver.query(name)
        except Exception as e:
            Scan.error('# [-] Host lookup failed for '+name,sys._getframe().f_code.co_name)
            pass

    @staticmethod
    def _ret_mx_by_name(name):
        #rdata.exchange for domains and rdata.preference for integer
        return [str(mx.exchange).rstrip('.') for mx in dns.resolver.query(name,'MX')]

    @staticmethod
    def _ret_ns_by_name(name):
        #rdata.exchange for domains and rdata.preference for integer
        return [str(ns).rstrip('.') for ns in dns.resolver.query(name,'NS')]
    
    def google_lookups(self):
        if self.domain:
            self.linkedin_page = self._get_linkedin_page_from_google(self.domain)
            self.subdomains = self._get_subdomains_from_google(self.domain)

    @staticmethod
    def _get_linkedin_page_from_google(name):
        try:
            request='http://google.com/search?hl=en&meta=&num=10&q=site:linkedin.com/company%20"'+name+'"'
            google_search = requests.get(request)
            google_results = re.findall('<cite>(.+?)<\/cite>', google_search.text)
            for url in google_results:
                if 'linkedin.com/company/' in url:
                    return re.sub('<.*?>', '', url)
                    
        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)

    @staticmethod
    def _get_subdomains_from_google(domain):

        def _google_subdomains_lookup(domain,subdomains_to_avoid,num,counter):
            #Returns list of unique subdomains

            #Sleep some time between 0 - 3.999 seconds
            time.sleep(randint(0,3)+randint(0,1000)*0.001)


            request = 'http://google.com/search?hl=en&meta=&num='+str(num)+'&start='+str(counter)+'&q='
            request = ''.join([request,'site%3A%2A',domain])

            for subdomain in subdomains_to_avoid:
                #Don't want to remove original name from google query
                if subdomain != domain:
                    request = ''.join([request,'%20%2Dsite%3A',str(subdomain)])

            google_search = None
            try:
                google_search = requests.get(request)
            except Exception as e:
                Scan.error(e,sys._getframe().f_code.co_name)

            new_subdomains = set()
            if google_search:
                google_results = re.findall('<cite>(.+?)<\/cite>', google_search.text)

                

                for url in set(google_results):
                    #Removing html tags from inside url (sometimes they ise <b> or <i> for ads)
                    url = re.sub('<.*?>', '', url)

                    #Follows Javascript pattern of accessing URLs
                    g_host = url
                    g_protocol = ''
                    g_pathname = ''

                    temp = url.split('://')

                    #If there is g_protocol e.g. http://, ftp://, etc
                    if len(temp)>1:
                        g_protocol = temp[0]
                        #remove g_protocol from url
                        url = ''.join(temp[1:])

                    temp = url.split('/')
                    #if there is a pathname after host
                    if len(temp)>1:

                        g_pathname = '/'.join(temp[1:])
                        g_host = temp[0]

                    new_subdomains.add(g_host)

                #TODO do something with g_pathname and g_protocol
                #Currently not using protocol or pathname for anything
            return list(new_subdomains)

        #Keeps subdomains found by _google_subdomains_lookup
        subdomains_discovered = []
        #Variable to check if there is any new result in the last iteration
        subdomains_in_last_iteration = -1

        while len(subdomains_discovered) > subdomains_in_last_iteration:
                                
            subdomains_in_last_iteration = len(subdomains_discovered)
            
            subdomains_discovered += _google_subdomains_lookup(domain,subdomains_discovered,100,0)
            subdomains_discovered = list(set(subdomains_discovered))

        subdomains_discovered += _google_subdomains_lookup(domain,subdomains_discovered,100,100)
        subdomains_discovered = list(set(subdomains_discovered))


        subdomains_to_add = set()
        
        domain = dns.name.from_text(domain)
        for subdomain in subdomains_discovered:
            try:
                #Only accepts valid domains - this will filter bad results
                sub_dns = dns.name.from_text(subdomain)
                #Tests if sub is really a subdomains from domain
                if sub_dns.is_subdomain(domain):
                    #If is valid dns and valid subdomain, will be added to results
                    subdomains_to_add.add(subdomain)
            except Exception as e:
                pass

        #Return set of valid results
        return set([ Host(domain=str(subdomain)).dns_lookups() for subdomain in subdomains_to_add ])

    def print_all_ips(self):
        ret = ''
        for ip in self.ips:
            ret = ''.join([ret,str(ip),'\n'])

            if ip.rev_domains:
                for rev in ip.rev_domains:
                    ret =  '\t'.join([ret,rev,'\n'])
        return ret.rstrip().lstrip()        
        
    
    def print_subdomains(self):
        return self._print_domains(sorted(self.subdomains,key=lambda x: x.domain))

    @staticmethod
    def _print_domains(domains):
        ret = ''
        for domain in domains:
            ret = ''.join([ret,domain.domain,'\n'])

            if domain.ips:
                for ip in domain.ips:
                    ret = '\t'.join([ret,str(ip),'\n'])

        return ret.rstrip().lstrip()

    def print_all_ns(self):
        return self._print_domains(self.ns)

    def print_all_mx(self):
        return self._print_domains(self.mx)


    @staticmethod
    def _print_subdomains_verbose(subdomains):
        try:
            result = ''
            for subdomain,value in sorted(subdomains.iteritems()):
                result = ''.join([result,subdomain,'\n'])
                for protocol,value2 in value.iteritems():
                    if len(value2)>0: 
                        #result = ''.join([result,'\t',protocol,'','\n'])
                        for pathname in value2:
                            if pathname: 
                                result = ''.join([result,'\t'])
                                if protocol: result = ''.join([result,protocol,'://'])
                                result = ''.join([result,subdomain,'/',pathname,'\n'])

            result = ''.join([result,'\n'])    

            return result.rstrip()

        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)

    def print_all_whois_ip(self):
        ret = ['# Results for CIDR '+cidr+':\n'+IP.print_whois_ip(whois_ip) for cidr,whois_ip in self.cidrs.iteritems() ]
        return '\n'.join(ret).lstrip().rstrip()


    def print_all_shodan(self):
        ret = [ ip.print_shodan() for ip in self.ips ]
        return '\n'.join(ret).lstrip().rstrip()


class IP(Host):
    '''
        IP and information specific to it.
        A Host can contain multiple IPs.
    '''

    def __init__(self,ip):
        self.ip = ip
        #Whois results for self.ips
        self.whois_ip = None
        #CIDR retrieved from whois_ip in get_specific_cidr()
        self.cidr = None
        #Reverse domains for IP, as str
        self.rev_domains = []
        #Shodan lookup for IP
        self.shodan = None

    def __str__(self):
        return str(self.ip)

    def __hash__(self):
        return hash(('ip',self.ip))
    
    def __eq__(self,other):
        return self.ip == other.ip

    @staticmethod
    def _ret_host_by_ip(ip):
        try:
            return dns.resolver.query(dns.reversename.from_address(ip),'PTR')
        except Exception as e:
            Scan.error('# [-] Host lookup failed for '+ip,sys._getframe().f_code.co_name)

    def get_rev_domains(self):
        try:
            rev_domains = None
            rev_domains = self._ret_host_by_ip(self.ip)
        
            if rev_domains:
                self.rev_domains = [ str(domain).rstrip('.') for domain in rev_domains ]

            return self

        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)
            pass

    def get_shodan(self, key):
        try:
            shodan_api_key = key
            api = shodan.Shodan(shodan_api_key)
            self.shodan = api.host(str(self))
        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)

    def get_whois_ip(self):

        raw_result = None
        try:
            raw_result = ipw(str(self)).lookup() or None
        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)
        
        if raw_result:
            result = {}

            if raw_result['nets']:
                #get results from parent 'nets'
                for index,net in enumerate(raw_result['nets']):
                    for key,val in net.iteritems():
                        if val:
                            result['net'+str(index)+'_'+key] = net[key].replace('\n',', ') \
                                if key in net and net[key] else None

            #get all remaining results not in 'nets'
            for key,val in raw_result.iteritems():
                if key not in ['nets','query']:
                    if type(val) in [dict]:

                        new_dict = {}
                        
                        for key2, val2 in value.iteritems():
                            if val2 != None:
                                new_dict[key2] = val2
                                result_is_valid = True

                        result[key] = new_dict

                    else:
                        if raw_result[key]:
                            result[key] = str(raw_result[key]).replace('\n',', ')
                            result_is_valid = True

            self.whois_ip = result
            
            if self.whois_ip:
                if 'net0_cidr' in self.whois_ip:
                    self.cidr = self.whois_ip['net0_cidr']

                    # for cidr in cidrs:
                    #     if (cidr.num_addresses < num_addresses) or (num_addresses == 0):
                    #         specific_cidr = cidr
                    #         num_addresses = cidr.num_addresses

                    #     #for biggest cidr do
                    #     #if (cidr.num_addresses > num_addresses):

            return self
    
    @staticmethod
    def print_whois_ip(whois_ip):
        try:
            if whois_ip:
                result = ''

                for key,val in sorted(whois_ip.iteritems()):
                    if val:
                        
                        if type(val) in [dict]:
                            for key2,val2 in val.iteritems():
                                result = '\n'.join([result,key+': '+str(val)]) #Improve, should print dicts right
                        
                        else:
                            result = '\n'.join([result,key+': '+str(val)])
                
                return result.lstrip().rstrip()

        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)
            pass

    def print_shodan(self):
        try:
            if self.shodan:

                result = ''.join(['IP: ',self.shodan.get('ip_str'),'\n'])
                result = ''.join([result,'Organization: ',self.shodan.get('org','n/a'),'\n'])

                if self.shodan.get('os','n/a'):
                    result = ''.join([result,'OS: ',self.shodan.get('os','n/a'),'\n'])

                if len(self.shodan['data']) > 0:
                    for item in self.shodan['data']:
                        result = ''.join([
                            
                            result,
                            'Port: {}'.format(item['port']),
                            '\n',
                            'Banner: {}'.format(item['data'].replace('\n','\n\t').rstrip()),

                            ])

                return result.rstrip().lstrip()

        except Exception as e:
            Scan.error(e,sys._getframe().f_code.co_name)

class Scan(object):
    '''
    Scan will hold all Host entries, manage scans, threads and outputs.
    '''

    feedback = False
    verbose = False

    def __init__(self,dns_server=None,shodan_key=None,feedback=False,verbose=False):

        #Print output
        Scan.feedback = feedback
        #Print verbosely
        Scan.verbose=verbose 


        self.dns_server = dns_server
        if self.dns_server:
            #Set DNS server - TODO test this to make sure server is really being changed
            dns.resolver.override_system_resolver(self.dns_server)

        #Shodan API key
        self.shodan_key = shodan_key

        #Targets that will be scanned
        self.targets = []
        #Malformed targets, or names that can't be resolved
        self.bad_targets = []
        #Secondary targets, deducted from CIDRs and other relations
        self.secondary_targets = []

    @staticmethod
    def error(e, function_name=None):
        if Scan.feedback and Scan.verbose: 
            print '# Error:', str(e),'| function:',function_name


    def populate(self, user_supplied_list):
        for user_supplied in user_supplied_list:
            self.add_host(user_supplied)

        if self.feedback:
            if len(self.targets)<1:
                print '# No hosts to scan'
            else:
                print '# Scanning',str(len(self.targets))+'/'+str(len(user_supplied_list)),'hosts'

                if not self.shodan_key:
                    print '# No Shodan key provided'
                else:
                    print'# Shodan key provided -',self.shodan_key


    def add_host(self, user_supplied, from_net=False):
        #is it an IP?
        try:
            ip = ipa.ip_address(user_supplied.decode('unicode-escape'))
            if not (ip.is_multicast or ip.is_unspecified or ip.is_reserved or ip.is_loopback):
                self.targets.append(Host(ips=[str(ip)]))
                return
            else:
                self.bad_targets.append(user_supplied)
                return
        except Exception as e:
            #print e
            pass

        #is it a valid network range?
        try:
            net = ipa.ip_network(user_supplied.decode('unicode-escape'),strict=False)
            if net.prefixlen < 16:
                if Scan.feedback: print '# [-] Error: Not a good idea to scan anything bigger than a /16 is it?', user_supplied
            else:
                for ip in net:
                    self.add_host(str(ip), True)
                return
                
        except Exception as e:
            #print e,type(e)
            pass

        #is it a valid DNS?
        try:
            pass
            domain = user_supplied
            ips = dns.resolver.query(user_supplied)
            self.targets.append(Host(domain=domain,ips=[str(ip) for ip in ips]))
            return
        except Exception as e:
            #If here so results from network won't be so verbose
            if not from_net:
                if Scan.feedback: print '# [-] Error: Couldn\'t resolve or understand', user_supplied
            pass

        self.bad_targets.append(user_supplied)


    def scan_targets(self):
        #Consists of DNS and whois lookups on the target hosts
        fb = Scan.feedback
        if len(self.targets)>0:

            for host in self.targets:
                
                if fb: print '\n____________________ Scanning {} ____________________\n'.format(str(host))
            

            ###DNS and Whois lookups###
                if fb: print '# DNS lookups'
                host.dns_lookups()
                if fb:
                    if host.domain:
                        print ''
                        print '# [*] Domain: '+host.domain
                    
                    #IPs and reverse domains
                    if host.ips: 
                        print ''
                        print '# [*] IPs & reverse DNS: '
                        print host.print_all_ips()
                    
                    
                host.ns_dns_lookup()
                #NS records
                if host.ns and fb:
                    print ''
                    print '# [*] NS records:'
                    print host.print_all_ns()


                host.mx_dns_lookup()
                #MX records
                if host.mx and fb:
                    print ''
                    print '# [*] MX records:'
                    print host.print_all_mx()


                if fb: 
                    print ''
                    print '# Whois lookups'

                host.get_whois_domain()
                if host.whois_domain and fb:
                    print '' 
                    print '# [*] Whois domain:'
                    print host.whois_domain

                host.get_all_whois_ip()
                if fb:
                    m = host.print_all_whois_ip()
                    if m:
                        print ''
                        print '# [*] Whois IP:'
                        print m
                return True
            #Shodan lookup
                if self.shodan_key:
                    
                    if fb: 
                        print ''
                        print '# Querying Shodan for open ports'

                    host.get_all_shodan(self.shodan_key)

                    if fb:
                        m = host.print_all_shodan()
                        if m:
                            print '# [*] Shodan:'
                            print m

            #Google subdomains lookup
                if host.domain:
                    if fb:
                        print '' 
                        print '# Querying Google for subdomains and Linkedin pages, this might take a while'
                    
                    host.google_lookups()
                    
                    if fb:
                        if host.linkedin_page:
                            print ''
                            print '# [*] Possible LinkedIn page: '+host.linkedin_page

                        if host.subdomains:
                            print ''
                            print '# [*] Subdomains:'+'\n'+host.print_subdomains()
                        else:
                            print '# [-] Error: No subdomains found in Google. If you are scanning a lot, Google might be blocking your requests.'


    def scan_cidrs(self):
        #DNS lookups on entire CIDRs taken from host.get_whois_ip()
        fb = self.feedback
        
        if len(self.targets)>0:

            for host in self.targets:
        
                if fb:
                    print ''
                    print '# Reverse DNS lookup on range(s) ',
                            +str(host.cidrs),
                            '(taken from',
                            str(host),
                            ')'
                
                for ip in host.cidrs:
                    secondary_target = IP(ip,feedback=False)
                    secondary_target.get_rev_domains()
                                            
                    
                    if secondary_target.rev_domains:

                        self.secondary_targets.append(secondary_target)
                    
                        if fb: print secondary_target.ip,secondary_target.rev_domains

            if fb: print "# Done"


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='InstaRecon')
    parser.add_argument('targets', nargs='+', help='targets')
    parser.add_argument('-d', '--dns_server', metavar='dns_server', required=False, nargs='?',help='DNS server to use')
    parser.add_argument('-s', '--shodan_key', metavar='shodan_key',required=False,nargs='?',help='Shodan key for automated lookups. To get one, simply register on https://www.shodan.io/.')
    parser.add_argument('-v','--verbose', action='store_true',help='Verbose output.')
    args = parser.parse_args()

    targets = sorted(set(args.targets)) #removes duplicates

    #instantiate Scan
    scan = Scan(
        dns_server=args.dns_server,
        shodan_key=args.shodan_key,
        feedback=True,
        verbose=args.verbose,
        )
    
    scan.populate(targets) #populate Scan with targets

    scan.scan_targets()

    #scan.scan_cidrs() #dns lookups on entire CIDRs that contain original targets
    


#TODO

    #fix cidr scanning

    #Debug google - test re.sub is all good



    
    #granularity in scanning - #reduce verbosity when doing only reverse lookups
    
    #print reverse domains
    
    #Output csv?
    
    #keyboard interrupt in secondary scan


    
